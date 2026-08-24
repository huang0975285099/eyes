package streamsource

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"regexp"
	"strings"
)

const (
	TypeScreen       = "screen"
	TypeUSBCamera    = "usb_camera"
	TypeIPCamera     = "ip_camera"
	TypeDirectCamera = "direct_camera"
)

var (
	compactMACPattern = regexp.MustCompile(`^[0-9a-f]{12}$`)
	sourceTypePattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,29}$`)
	sourceHashPattern = regexp.MustCompile(`^[0-9a-f]{12}$`)
)

// Normalize validates and normalizes the source identity supplied by a client.
func Normalize(mac, sourceType, sourceID, displayName string) (string, string, string, string, error) {
	mac = strings.ToLower(strings.ReplaceAll(strings.TrimSpace(mac), "-", ":"))
	parsed, err := net.ParseMAC(mac)
	if err != nil || len(parsed) != 6 {
		return "", "", "", "", fmt.Errorf("invalid MAC address")
	}

	sourceType = strings.ToLower(strings.TrimSpace(sourceType))
	if sourceType == "" {
		return "", "", "", "", fmt.Errorf("source_type is required")
	}
	if !sourceTypePattern.MatchString(sourceType) {
		return "", "", "", "", fmt.Errorf("unsupported source type %q", sourceType)
	}

	sourceID = strings.TrimSpace(sourceID)
	if sourceID == "" {
		return "", "", "", "", fmt.Errorf("source_id is required")
	}
	if len([]rune(sourceID)) > 100 {
		return "", "", "", "", fmt.Errorf("source_id is too long")
	}

	displayName = strings.TrimSpace(displayName)
	if displayName == "" {
		switch sourceType {
		case TypeScreen:
			displayName = "电脑桌面"
		case TypeUSBCamera:
			displayName = "USB 摄像头"
		case TypeIPCamera:
			displayName = "网络摄像头"
		default:
			displayName = "摄像头"
		}
	}
	if len([]rune(displayName)) > 100 {
		return "", "", "", "", fmt.Errorf("display_name is too long")
	}
	return mac, sourceType, sourceID, displayName, nil
}

// Name returns a stable, SRS/filesystem-safe stream name. Source IDs are
// represented by a hash and never expose RTSP URLs, credentials, or vendor
// specific device names.
func Name(mac, sourceType, sourceID string) string {
	compact := strings.ReplaceAll(strings.ToLower(mac), ":", "")
	typePart := strings.ReplaceAll(sourceType, "_", "-")
	sum := sha256.Sum256([]byte(sourceID))
	return fmt.Sprintf("%s--%s--%s", compact, typePart, hex.EncodeToString(sum[:6]))
}

// DirectName creates the permanent stream name used by a standalone camera
// that publishes directly to SRS without going through the Electron client.
func DirectName(sourceID string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(sourceID)))
	return "camera--" + hex.EncodeToString(sum[:8])
}

func NormalizeDirect(sourceID, displayName, brand string) (string, string, string, error) {
	sourceID = strings.TrimSpace(sourceID)
	if sourceID == "" {
		return "", "", "", fmt.Errorf("source_id is required")
	}
	if len([]rune(sourceID)) > 100 {
		return "", "", "", fmt.Errorf("source_id is too long")
	}
	displayName = strings.TrimSpace(displayName)
	if displayName == "" {
		return "", "", "", fmt.Errorf("display_name is required")
	}
	if len([]rune(displayName)) > 100 {
		return "", "", "", fmt.Errorf("display_name is too long")
	}
	brand = strings.TrimSpace(brand)
	if len([]rune(brand)) > 100 {
		return "", "", "", fmt.Errorf("brand is too long")
	}
	return sourceID, displayName, brand, nil
}

// Parse recovers routing metadata from a multi-source stream name. SourceID
// and display name are stored in the database because the public stream name
// only contains a non-sensitive hash.
func Parse(name string) (mac, sourceType string, ok bool) {
	name = strings.ToLower(strings.TrimSpace(strings.TrimSuffix(name, ".flv")))
	parts := strings.Split(name, "--")
	compact := parts[0]
	if !compactMACPattern.MatchString(compact) {
		return "", "", false
	}
	mac = fmt.Sprintf("%s:%s:%s:%s:%s:%s",
		compact[0:2], compact[2:4], compact[4:6], compact[6:8], compact[8:10], compact[10:12])
	if len(parts) != 3 || !sourceHashPattern.MatchString(parts[2]) {
		return "", "", false
	}
	sourceType = strings.ReplaceAll(parts[1], "-", "_")
	if !sourceTypePattern.MatchString(sourceType) {
		return "", "", false
	}
	return mac, sourceType, true
}
