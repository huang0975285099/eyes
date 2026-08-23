package streamsource

import (
	"strings"
	"testing"
)

func TestLegacyDesktopNameAndParse(t *testing.T) {
	name := Name("d8:5e:d3:9f:2a:17", TypeScreen, "desktop")
	if name != "d85ed39f2a17" {
		t.Fatalf("unexpected legacy name %q", name)
	}
	mac, sourceType, ok := Parse(name)
	if !ok || mac != "d8:5e:d3:9f:2a:17" || sourceType != TypeScreen {
		t.Fatalf("unexpected parse result: %q %q %v", mac, sourceType, ok)
	}
}

func TestCameraNamesAreStableAndDoNotLeakSourceID(t *testing.T) {
	name := Name("d8:5e:d3:9f:2a:17", TypeIPCamera, "rtsp://admin:secret@camera/live")
	if name != "d85ed39f2a17--ip-camera--69f9ade21f4e" {
		t.Fatalf("unexpected camera name %q", name)
	}
	mac, sourceType, ok := Parse(name)
	if !ok || mac != "d8:5e:d3:9f:2a:17" || sourceType != TypeIPCamera {
		t.Fatalf("unexpected parse result: %q %q %v", mac, sourceType, ok)
	}
}

func TestNormalizeRequiresCameraID(t *testing.T) {
	_, _, _, _, err := Normalize("d8:5e:d3:9f:2a:17", TypeUSBCamera, "", "")
	if err == nil {
		t.Fatal("expected missing source_id to fail")
	}
}

func TestFutureVendorSourceTypeDoesNotRequireServerChanges(t *testing.T) {
	mac, sourceType, sourceID, _, err := Normalize(
		"d8:5e:d3:9f:2a:17", "vendor_camera", "vendor-01", "Vendor Camera",
	)
	if err != nil {
		t.Fatal(err)
	}
	name := Name(mac, sourceType, sourceID)
	parsedMAC, parsedType, ok := Parse(name)
	if !ok || parsedMAC != mac || parsedType != sourceType {
		t.Fatalf("unexpected parse result: %q %q %v", parsedMAC, parsedType, ok)
	}
}

func TestDirectCameraNameIsPermanentAndSafe(t *testing.T) {
	name := DirectName("north-gate")
	if name != "camera--56c8c63d3f7abfbd" {
		t.Fatalf("unexpected direct camera name %q", name)
	}
	if strings.Contains(name, "north-gate") {
		t.Fatal("direct camera name leaked source ID")
	}
}
