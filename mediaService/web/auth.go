package web

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"media-service/database"
	"media-service/models"
	"net/http"
	"regexp"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"
	"gorm.io/gorm"
)

const (
	rolePlatformAdmin = "platform_admin"
	roleCustomerAdmin = "customer_admin"
	sessionLifetime   = 7 * 24 * time.Hour
)

var usernamePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{3,64}$`)

type principal struct {
	User      models.User
	Customer  *models.Customer
	TokenHash string
}

func (p principal) isPlatformAdmin() bool { return p.User.Role == rolePlatformAdmin }

func (s *Server) handleAuthStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var count int64
	if err := database.DB.Model(&models.User{}).Count(&count).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询初始化状态失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"setup_required": count == 0})
}

func (s *Server) handleAuthSetup(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	username, passwordHash, err := validatedCredentials(req.Username, req.Password)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	var user models.User
	err = database.DB.Transaction(func(tx *gorm.DB) error {
		var count int64
		if err := tx.Model(&models.User{}).Count(&count).Error; err != nil {
			return err
		}
		if count != 0 {
			return errSetupCompleted
		}
		user = models.User{Username: username, PasswordHash: passwordHash, Role: rolePlatformAdmin, Enabled: true}
		return tx.Create(&user).Error
	})
	if errors.Is(err, errSetupCompleted) {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "系统已经完成初始化"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "创建平台管理员失败"})
		return
	}
	s.issueSession(w, user)
}

func (s *Server) handleAuthLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "账号或密码错误"})
		return
	}
	var user models.User
	if err := database.DB.Where("username = ? AND enabled = ?", strings.TrimSpace(req.Username), true).First(&user).Error; err != nil ||
		bcrypt.CompareHashAndPassword([]byte(user.PasswordHash), []byte(req.Password)) != nil {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "账号或密码错误"})
		return
	}
	if user.CustomerID != 0 {
		var customer models.Customer
		if err := database.DB.Where("id = ? AND enabled = ?", user.CustomerID, true).First(&customer).Error; err != nil {
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "客户账号已停用"})
			return
		}
	}
	s.issueSession(w, user)
}

func (s *Server) handleAuthMe(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, principalPayload(p))
}

func (s *Server) handleAuthLogout(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	database.DB.Where("token_hash = ?", p.TokenHash).Delete(&models.UserSession{})
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) handleAuthPassword(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	p, ok := requirePrincipal(w, r)
	if !ok {
		return
	}
	var req struct {
		CurrentPassword string `json:"current_password"`
		NewPassword     string `json:"new_password"`
	}
	if err := decodeJSONBody(r, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "密码参数无效"})
		return
	}
	if bcrypt.CompareHashAndPassword([]byte(p.User.PasswordHash), []byte(req.CurrentPassword)) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "当前密码错误"})
		return
	}
	_, passwordHash, err := validatedCredentials(p.User.Username, req.NewPassword)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if err := database.DB.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&models.User{}).Where("id = ?", p.User.ID).Update("password_hash", passwordHash).Error; err != nil {
			return err
		}
		return tx.Where("user_id = ?", p.User.ID).Delete(&models.UserSession{}).Error
	}); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "修改密码失败"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true, "login_required": true})
}

func (s *Server) issueSession(w http.ResponseWriter, user models.User) {
	// Expired rows are no longer useful and can otherwise grow forever on a
	// frequently used customer portal.
	database.DB.Where("expires_at <= ?", time.Now()).Delete(&models.UserSession{})
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "创建登录会话失败"})
		return
	}
	token := base64.RawURLEncoding.EncodeToString(raw)
	hash := tokenHash(token)
	session := models.UserSession{TokenHash: hash, UserID: user.ID, ExpiresAt: time.Now().Add(sessionLifetime)}
	if err := database.DB.Create(&session).Error; err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "保存登录会话失败"})
		return
	}
	p := principal{User: user, TokenHash: hash}
	if user.CustomerID != 0 {
		var customer models.Customer
		if err := database.DB.First(&customer, user.CustomerID).Error; err == nil {
			p.Customer = &customer
		}
	}
	payload := principalPayload(p)
	payload["session_token"] = token
	payload["expires_at"] = session.ExpiresAt
	writeJSON(w, http.StatusOK, payload)
}

func requirePrincipal(w http.ResponseWriter, r *http.Request) (principal, bool) {
	p, err := authenticateRequest(r)
	if err != nil {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "请先登录"})
		return principal{}, false
	}
	return p, true
}

func authenticateRequest(r *http.Request) (principal, error) {
	authorization := strings.TrimSpace(r.Header.Get("Authorization"))
	if !strings.HasPrefix(strings.ToLower(authorization), "bearer ") {
		return principal{}, errors.New("missing bearer token")
	}
	token := strings.TrimSpace(authorization[len("Bearer "):])
	if token == "" {
		return principal{}, errors.New("empty bearer token")
	}
	hash := tokenHash(token)
	var session models.UserSession
	if err := database.DB.Where("token_hash = ? AND expires_at > ?", hash, time.Now()).First(&session).Error; err != nil {
		return principal{}, err
	}
	var user models.User
	if err := database.DB.Where("id = ? AND enabled = ?", session.UserID, true).First(&user).Error; err != nil {
		return principal{}, err
	}
	p := principal{User: user, TokenHash: hash}
	if user.CustomerID != 0 {
		var customer models.Customer
		if err := database.DB.Where("id = ? AND enabled = ?", user.CustomerID, true).First(&customer).Error; err != nil {
			return principal{}, err
		}
		p.Customer = &customer
	}
	return p, nil
}

func principalPayload(p principal) map[string]any {
	customerName := ""
	if p.Customer != nil {
		customerName = p.Customer.Name
	}
	return map[string]any{
		"user": map[string]any{
			"id": p.User.ID, "username": p.User.Username, "role": p.User.Role,
			"customer_id": p.User.CustomerID, "customer_name": customerName,
		},
	}
}

func validatedCredentials(username, password string) (string, string, error) {
	username = strings.TrimSpace(username)
	if !usernamePattern.MatchString(username) {
		return "", "", errors.New("账号必须为3到64位字母、数字、点、下划线或横线")
	}
	if len(password) < 8 || len(password) > 72 {
		return "", "", errors.New("密码长度必须为8到72个字符")
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return "", "", errors.New("密码处理失败")
	}
	return username, string(hash), nil
}

func tokenHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

var errSetupCompleted = errors.New("setup already completed")
