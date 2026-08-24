package web

import "testing"

func TestNormalizePublishMetadata(t *testing.T) {
	operatorName, hostname, localIP, err := normalizePublishMetadata(publishConfigRequest{
		OperatorName: " 张三 ",
		Hostname:     " PC-01 ",
		LocalIP:      " 10.0.20.59 ",
	})
	if err != nil {
		t.Fatalf("normalizePublishMetadata returned error: %v", err)
	}
	if operatorName != "张三" || hostname != "PC-01" || localIP != "10.0.20.59" {
		t.Fatalf("unexpected normalized metadata: %q %q %q", operatorName, hostname, localIP)
	}

	if _, _, _, err := normalizePublishMetadata(publishConfigRequest{LocalIP: "not-an-ip"}); err == nil {
		t.Fatal("expected invalid IP to fail")
	}
	if _, _, _, err := normalizePublishMetadata(publishConfigRequest{OperatorName: "123456789012345678901"}); err == nil {
		t.Fatal("expected long operator name to fail")
	}
}
