package web

import (
	"archive/zip"
	"crypto/sha512"
	"crypto/subtle"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const maxUpdateUploadSize = 1024 << 20 // 1 GiB

var (
	versionLine = regexp.MustCompile(`(?m)^version:\s*["']?([^\s"']+)`)
	pathLine    = regexp.MustCompile(`(?m)^path:\s*["']?([^\r\n"']+)`)
	sha512Line  = regexp.MustCompile(`(?m)^sha512:\s*["']?([^\s"']+)`)
)

type clientUpdateManifest struct {
	Version     string `json:"version"`
	Path        string `json:"path"`
	SHA512      string `json:"sha512"`
	DownloadURL string `json:"download_url"`
}

func updateDir() string {
	if value := strings.TrimSpace(os.Getenv("CLIENT_UPDATE_DIR")); value != "" {
		return value
	}
	return "/var/client-updates"
}

func (s *Server) handleClientUpdateLatest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	manifest, err := readClientUpdateManifest(updateDir())
	if err != nil {
		if os.IsNotExist(err) {
			writeJSON(w, http.StatusNotFound, map[string]any{"message": "暂无客户端更新"})
			return
		}
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": err.Error()})
		return
	}
	manifest.DownloadURL = "/client-updates/" + manifest.Path
	writeJSON(w, http.StatusOK, manifest)
}

func (s *Server) handleClientUpdateUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	expected := strings.TrimSpace(os.Getenv("UPDATE_ADMIN_KEY"))
	provided := r.Header.Get("X-Update-Key")
	if expected == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"message": "UPDATE_ADMIN_KEY 未配置"})
		return
	}
	if len(provided) != len(expected) || subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) != 1 {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "更新管理密钥无效"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxUpdateUploadSize)
	if err := r.ParseMultipartForm(32 << 20); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "ZIP 上传数据无效或文件过大"})
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "缺少 file 字段"})
		return
	}
	defer file.Close()
	if !strings.EqualFold(filepath.Ext(header.Filename), ".zip") {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": "只允许上传 ZIP 文件"})
		return
	}
	dir := updateDir()
	if err := os.MkdirAll(dir, 0755); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "创建更新目录失败"})
		return
	}
	tmp, err := os.CreateTemp(dir, "upload-*.zip")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "保存上传文件失败"})
		return
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err = io.Copy(tmp, file); err != nil {
		tmp.Close()
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "保存上传文件失败"})
		return
	}
	if err = tmp.Close(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "保存上传文件失败"})
		return
	}
	staging, err := os.MkdirTemp(dir, ".update-staging-*")
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": "创建更新暂存目录失败"})
		return
	}
	defer os.RemoveAll(staging)
	if err = extractClientUpdateZIP(tmpName, staging); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": err.Error()})
		return
	}
	manifest, err := readClientUpdateManifest(staging)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"message": err.Error()})
		return
	}
	if err = publishClientUpdate(staging, dir, manifest.Path); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"message": err.Error()})
		return
	}
	manifest.DownloadURL = "/client-updates/" + manifest.Path
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "update": manifest})
}

func publishClientUpdate(staging, destination, installerName string) error {
	// 安装包先发布，latest.yml 最后发布，避免客户端读到尚未就绪的版本。
	files := []string{installerName, "builder-effective-config.yaml", "latest.yml"}
	for _, name := range files {
		source := filepath.Join(staging, name)
		if _, err := os.Stat(source); err != nil {
			if os.IsNotExist(err) && name == "builder-effective-config.yaml" {
				continue
			}
			return fmt.Errorf("发布 %s 失败: %w", name, err)
		}
		if err := os.Rename(source, filepath.Join(destination, name)); err != nil {
			return fmt.Errorf("发布 %s 失败: %w", name, err)
		}
	}
	return nil
}

func extractClientUpdateZIP(zipPath, destination string) error {
	reader, err := zip.OpenReader(zipPath)
	if err != nil {
		return fmt.Errorf("无法读取 ZIP: %w", err)
	}
	defer reader.Close()
	allowed := map[string]bool{"latest.yml": true, "builder-effective-config.yaml": true}
	extracted := map[string]bool{}
	for _, entry := range reader.File {
		name := filepath.Base(filepath.Clean(entry.Name))
		if entry.FileInfo().IsDir() || name == "." || name == "" {
			continue
		}
		if !allowed[name] && !strings.HasSuffix(strings.ToLower(name), "-setup.exe") {
			continue
		}
		rc, err := entry.Open()
		if err != nil {
			return fmt.Errorf("读取 ZIP 文件失败: %w", err)
		}
		target := filepath.Join(destination, name)
		out, err := os.OpenFile(target+".tmp", os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0644)
		if err == nil {
			_, err = io.Copy(out, rc)
		}
		if out != nil {
			_ = out.Close()
		}
		_ = rc.Close()
		if err != nil {
			return fmt.Errorf("解压 %s 失败: %w", name, err)
		}
		if err = os.Rename(target+".tmp", target); err != nil {
			return fmt.Errorf("发布 %s 失败: %w", name, err)
		}
		extracted[name] = true
	}
	if !extracted["latest.yml"] {
		return fmt.Errorf("ZIP 中缺少 latest.yml")
	}
	return nil
}

func readClientUpdateManifest(dir string) (clientUpdateManifest, error) {
	data, err := os.ReadFile(filepath.Join(dir, "latest.yml"))
	if err != nil {
		return clientUpdateManifest{}, err
	}
	version := firstMatch(versionLine, data)
	path := filepath.Base(strings.TrimSpace(firstMatch(pathLine, data)))
	expectedSHA512 := firstMatch(sha512Line, data)
	if version == "" || path == "" || expectedSHA512 == "" {
		return clientUpdateManifest{}, fmt.Errorf("latest.yml 缺少 version、path 或 sha512")
	}
	if !strings.HasSuffix(strings.ToLower(path), ".exe") {
		return clientUpdateManifest{}, fmt.Errorf("latest.yml 的 path 必须指向 EXE 安装包")
	}
	if _, err := os.Stat(filepath.Join(dir, path)); err != nil {
		return clientUpdateManifest{}, fmt.Errorf("ZIP 中缺少安装包 %s", path)
	}
	installer, err := os.Open(filepath.Join(dir, path))
	if err != nil {
		return clientUpdateManifest{}, fmt.Errorf("读取安装包 %s 失败: %w", path, err)
	}
	hasher := sha512.New()
	_, hashErr := io.Copy(hasher, installer)
	closeErr := installer.Close()
	if hashErr != nil {
		return clientUpdateManifest{}, fmt.Errorf("校验安装包 %s 失败: %w", path, hashErr)
	}
	if closeErr != nil {
		return clientUpdateManifest{}, fmt.Errorf("关闭安装包 %s 失败: %w", path, closeErr)
	}
	actualSHA512 := base64.StdEncoding.EncodeToString(hasher.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(actualSHA512), []byte(expectedSHA512)) != 1 {
		return clientUpdateManifest{}, fmt.Errorf("安装包 %s 的 SHA512 与 latest.yml 不一致", path)
	}
	return clientUpdateManifest{Version: version, Path: path, SHA512: expectedSHA512}, nil
}

func firstMatch(expression *regexp.Regexp, data []byte) string {
	match := expression.FindSubmatch(data)
	if len(match) < 2 {
		return ""
	}
	return strings.TrimSpace(string(match[1]))
}
