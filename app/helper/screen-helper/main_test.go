//go:build windows

package main

import "testing"

func argValue(args []string, key string) (string, bool) {
	for i := 0; i+1 < len(args); i++ {
		if args[i] == key {
			return args[i+1], true
		}
	}
	return "", false
}

func TestBuildFFmpegArgsHardwareKeepsNativeResolution(t *testing.T) {
	args, scaled := buildFFmpegArgs("h264_nvenc", "bgra", 2560, 1440, 15, "rtmp://example/live/test")
	if scaled {
		t.Fatal("hardware encoding must not enable software scaling")
	}
	if _, ok := argValue(args, "-vf"); ok {
		t.Fatal("hardware encoding unexpectedly contains a scale filter")
	}
	if got, _ := argValue(args, "-s"); got != "2560x1440" {
		t.Fatalf("hardware input resolution = %q, want 2560x1440", got)
	}
	if got, _ := argValue(args, "-b:v"); got != "4000k" {
		t.Fatalf("hardware bitrate = %q, want 4000k", got)
	}
}

func TestBuildFFmpegArgsLibx264ScalesTo1280(t *testing.T) {
	args, scaled := buildFFmpegArgs("libx264", "bgra", 1920, 1080, 15, "rtmp://example/live/test")
	if !scaled {
		t.Fatal("libx264 must scale sources wider than 1280")
	}
	if got, _ := argValue(args, "-vf"); got != "scale=1280:-2" {
		t.Fatalf("libx264 filter = %q, want scale=1280:-2", got)
	}
	if got, _ := argValue(args, "-s"); got != "1920x1080" {
		t.Fatalf("raw input resolution = %q, want 1920x1080", got)
	}
	if got, _ := argValue(args, "-b:v"); got != "1800k" {
		t.Fatalf("scaled libx264 bitrate = %q, want 1800k", got)
	}
}

func TestBuildFFmpegArgsLibx264DoesNotUpscale(t *testing.T) {
	args, scaled := buildFFmpegArgs("libx264", "bgra", 1024, 768, 8, "rtmp://example/live/test")
	if scaled {
		t.Fatal("libx264 must not upscale sources at or below 1280 pixels wide")
	}
	if _, ok := argValue(args, "-vf"); ok {
		t.Fatal("small libx264 source unexpectedly contains a scale filter")
	}
}
