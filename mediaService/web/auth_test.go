package web

import (
	"testing"

	"golang.org/x/crypto/bcrypt"
)

func TestValidatedCredentials(t *testing.T) {
	username, hash, err := validatedCredentials(" customer_01 ", "StrongPass123")
	if err != nil {
		t.Fatalf("validatedCredentials returned error: %v", err)
	}
	if username != "customer_01" {
		t.Fatalf("unexpected normalized username: %q", username)
	}
	if bcrypt.CompareHashAndPassword([]byte(hash), []byte("StrongPass123")) != nil {
		t.Fatal("password hash does not verify")
	}
	if _, _, err := validatedCredentials("x", "StrongPass123"); err == nil {
		t.Fatal("expected short username to fail")
	}
	if _, _, err := validatedCredentials("customer", "short"); err == nil {
		t.Fatal("expected short password to fail")
	}
}

func TestTokenHashDoesNotStoreRawSession(t *testing.T) {
	hash := tokenHash("session-secret")
	if hash == "session-secret" || len(hash) != 64 {
		t.Fatalf("unexpected token hash: %q", hash)
	}
}
