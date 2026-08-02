package web

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestStreamToken(t *testing.T) {
	secret := "test-secret"
	stream := "d85ed39f2a17"
	now := time.Unix(1_800_000_000, 0)
	token := signStreamToken(secret, stream, now.Add(time.Hour).Unix())
	if !verifyStreamToken(secret, stream, token, now) {
		t.Fatal("fresh token should be valid")
	}
	if verifyStreamToken(secret, "001122334455", token, now) {
		t.Fatal("token must be bound to the stream name")
	}
	if verifyStreamToken(secret, stream, token, now.Add(2*time.Hour)) {
		t.Fatal("expired token should be rejected")
	}
	if verifyStreamToken("another-secret", stream, token, now) {
		t.Fatal("token signed with another secret should be rejected")
	}
}

func TestPublishConfigAndSRSHook(t *testing.T) {
	server := &Server{
		ClientAPIKey:   "client-key",
		StreamSecret:   "stream-secret",
		PublicRTMPHost: "112.18.238.6:21935",
		SRSHttpHost:    "112.18.238.6:28080",
	}
	request := httptest.NewRequest(http.MethodPost, "/api/streams/publish-config", strings.NewReader(`{"mac":"D8-5E-D3-9F-2A-17"}`))
	request.Header.Set("X-Client-Key", "client-key")
	response := httptest.NewRecorder()
	server.handlePublishConfig(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("publish config returned %d: %s", response.Code, response.Body.String())
	}
	var config struct {
		RTMPURL string `json:"rtmp_url"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &config); err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(config.RTMPURL)
	if err != nil || parsed.Host != "112.18.238.6:21935" || parsed.Path != "/live/d85ed39f2a17" {
		t.Fatalf("unexpected RTMP URL: %s", config.RTMPURL)
	}

	hookBody, _ := json.Marshal(srsHookRequest{App: "live", Stream: "d85ed39f2a17", Param: "?" + parsed.RawQuery})
	hookRequest := httptest.NewRequest(http.MethodPost, "/api/srs/on-publish", bytes.NewReader(hookBody))
	hookResponse := httptest.NewRecorder()
	server.handleSRSPublish(hookResponse, hookRequest)
	if !strings.Contains(hookResponse.Body.String(), `"code":0`) {
		t.Fatalf("valid SRS publish was rejected: %s", hookResponse.Body.String())
	}
}
