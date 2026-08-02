//go:build windows

package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"image"
	"image/jpeg"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/gorilla/websocket"
)

// ── COM / D3D11 / DXGI types ─────────────────────────────────────────────────

type guid struct {
	Data1 uint32
	Data2 uint16
	Data3 uint16
	Data4 [8]byte
}

var (
	iidIDXGIDevice     = guid{0x54ec77fa, 0x1377, 0x44e6, [8]byte{0x8c, 0x32, 0x88, 0xfd, 0x5f, 0x44, 0xc8, 0x4c}}
	iidIDXGIOutput1    = guid{0x00cddea8, 0x939b, 0x4b83, [8]byte{0xa3, 0x40, 0xa6, 0x85, 0x22, 0x66, 0x66, 0xcc}}
	iidIDXGIResource   = guid{0x035f3ab4, 0x482a, 0x4255, [8]byte{0x89, 0x87, 0x4e, 0x9b, 0x54, 0x9b, 0x29, 0xfe}}
	iidID3D11Texture2D = guid{0x6f15aaf2, 0xd208, 0x4e89, [8]byte{0x9a, 0xb4, 0x48, 0x95, 0x35, 0xd3, 0x4f, 0x9c}}
)

var (
	d3d11dll              = syscall.NewLazyDLL("d3d11.dll")
	procD3D11CreateDevice = d3d11dll.NewProc("D3D11CreateDevice")
)

var (
	user32dll            = syscall.NewLazyDLL("user32.dll")
	procSendInput        = user32dll.NewProc("SendInput")
	procGetSystemMetrics = user32dll.NewProc("GetSystemMetrics")
	procGetDC            = user32dll.NewProc("GetDC")
	procReleaseDC        = user32dll.NewProc("ReleaseDC")
)

var (
	gdi32dll               = syscall.NewLazyDLL("gdi32.dll")
	procCreateCompatibleDC = gdi32dll.NewProc("CreateCompatibleDC")
	procCreateDIBSection   = gdi32dll.NewProc("CreateDIBSection")
	procSelectObject       = gdi32dll.NewProc("SelectObject")
	procBitBlt             = gdi32dll.NewProc("BitBlt")
	procDeleteObject       = gdi32dll.NewProc("DeleteObject")
	procDeleteDC           = gdi32dll.NewProc("DeleteDC")
)

var screenW, screenH uint32

const (
	d3dDriverTypeHardware   = 1
	d3d11SDKVersion         = 7
	dxgiFormatB8G8R8A8Unorm = 87
	d3d11UsageStaging       = 3
	d3d11CPUAccessRead      = 0x20000
	d3d11MapRead            = 1
	dxgiErrorWaitTimeout    = 0x887A0027
	dxgiErrorAccessLost     = 0x887A0026
	dxgiErrorInvalidCall    = 0x887A0001
)

// Win32 SendInput constants (mouse + keyboard)
const (
	inputTypeMouse    uint32 = 0
	inputTypeKeyboard uint32 = 1

	meMove       uint32 = 0x0001
	meAbsolute   uint32 = 0x8000
	meLeftDown   uint32 = 0x0002
	meLeftUp     uint32 = 0x0004
	meRightDown  uint32 = 0x0008
	meRightUp    uint32 = 0x0010
	meMiddleDown uint32 = 0x0020
	meMiddleUp   uint32 = 0x0040
	meWheel      uint32 = 0x0800

	keyEventfKeyUp uint32 = 0x0002
)

// comVtbl returns the vtable entry at idx for a COM object pointer.
func comVtbl(obj uintptr, idx int) uintptr {
	vtbl := *(*uintptr)(unsafe.Pointer(obj))
	return *(*uintptr)(unsafe.Pointer(vtbl + uintptr(idx)*8))
}

func comCall(obj uintptr, idx int, args ...uintptr) uintptr {
	fn := comVtbl(obj, idx)
	all := make([]uintptr, 1+len(args))
	all[0] = obj
	copy(all[1:], args)
	r, _, _ := syscall.SyscallN(fn, all...)
	return r
}

func comRelease(obj uintptr) {
	if obj != 0 {
		comCall(obj, 2) // IUnknown::Release
	}
}

func comQueryInterface(obj uintptr, iid *guid) (uintptr, error) {
	var out uintptr
	hr := comCall(obj, 0, uintptr(unsafe.Pointer(iid)), uintptr(unsafe.Pointer(&out)))
	if hr != 0 {
		return 0, fmt.Errorf("QueryInterface HRESULT 0x%08X", uint32(hr))
	}
	return out, nil
}

// ── Structs matching Windows ABI ──────────────────────────────────────────────

// DXGI_OUTDUPL_DESC (36 bytes)
type dxgiOutduplDesc struct {
	Width                      uint32
	Height                     uint32
	RefreshRateNumerator       uint32
	RefreshRateDenominator     uint32
	Format                     uint32
	ScanlineOrdering           uint32
	Scaling                    uint32
	Rotation                   uint32
	DesktopImageInSystemMemory uint32
}

// DXGI_OUTDUPL_FRAME_INFO (48 bytes)
type dxgiOutduplFrameInfo struct {
	LastPresentTime           int64
	LastMouseUpdateTime       int64
	AccumulatedFrames         uint32
	RectsCoalesced            uint32
	ProtectedContentMaskedOut uint32
	PointerPositionX          int32
	PointerPositionY          int32
	PointerPositionVisible    uint32
	TotalMetadataBufferSize   uint32
	PointerShapeBufferSize    uint32
}

// D3D11_TEXTURE2D_DESC (44 bytes)
type d3d11Texture2DDesc struct {
	Width          uint32
	Height         uint32
	MipLevels      uint32
	ArraySize      uint32
	Format         uint32
	SampleCount    uint32
	SampleQuality  uint32
	Usage          uint32
	BindFlags      uint32
	CPUAccessFlags uint32
	MiscFlags      uint32
}

// D3D11_MAPPED_SUBRESOURCE (16 bytes on 64-bit)
type d3d11MappedSubresource struct {
	PData      uintptr
	RowPitch   uint32
	DepthPitch uint32
}

// DXGI_ADAPTER_DESC (304 bytes on 64-bit)
type dxgiAdapterDesc struct {
	Description           [128]uint16
	VendorId              uint32
	DeviceId              uint32
	SubSysId              uint32
	Revision              uint32
	DedicatedVideoMemory  uintptr
	DedicatedSystemMemory uintptr
	SharedSystemMemory    uintptr
	AdapterLuidLow        uint32
	AdapterLuidHigh       uint32
}

// ── DDA capture ───────────────────────────────────────────────────────────────

type DDA struct {
	device uintptr // ID3D11Device*
	ctx    uintptr // ID3D11DeviceContext*
	dupl   uintptr // IDXGIOutputDuplication*
	stage  uintptr // ID3D11Texture2D* staging
	width  uint32
	height uint32
	imgBuf *image.RGBA // reused across Capture calls to avoid per-frame 8 MB alloc
}

func newDDA() (*DDA, error) {
	var device, ctx uintptr
	hr, _, _ := procD3D11CreateDevice.Call(
		0, d3dDriverTypeHardware, 0, 0, 0, 0,
		d3d11SDKVersion,
		uintptr(unsafe.Pointer(&device)),
		0,
		uintptr(unsafe.Pointer(&ctx)),
	)
	if hr != 0 {
		return nil, fmt.Errorf("D3D11CreateDevice: 0x%08X", uint32(hr))
	}

	// ID3D11Device → IDXGIDevice → adapter → output → IDXGIOutput1 → duplication
	dxgiDev, err := comQueryInterface(device, &iidIDXGIDevice)
	if err != nil {
		comRelease(device)
		comRelease(ctx)
		return nil, err
	}

	// IDXGIDevice::GetAdapter (vtable idx 7)
	var adapter uintptr
	if hr = comCall(dxgiDev, 7, uintptr(unsafe.Pointer(&adapter))); hr != 0 {
		comRelease(dxgiDev)
		comRelease(device)
		comRelease(ctx)
		return nil, fmt.Errorf("GetAdapter: 0x%08X", uint32(hr))
	}
	comRelease(dxgiDev)

	// IDXGIAdapter::EnumOutputs(0) (vtable idx 7)
	var output uintptr
	if hr = comCall(adapter, 7, 0, uintptr(unsafe.Pointer(&output))); hr != 0 {
		comRelease(adapter)
		comRelease(device)
		comRelease(ctx)
		return nil, fmt.Errorf("EnumOutputs: 0x%08X", uint32(hr))
	}
	comRelease(adapter)

	// IDXGIOutput → IDXGIOutput1
	output1, err := comQueryInterface(output, &iidIDXGIOutput1)
	comRelease(output)
	if err != nil {
		comRelease(device)
		comRelease(ctx)
		return nil, err
	}

	// IDXGIOutput1::DuplicateOutput (vtable idx 22)
	var dupl uintptr
	if hr = comCall(output1, 22, device, uintptr(unsafe.Pointer(&dupl))); hr != 0 {
		comRelease(output1)
		comRelease(device)
		comRelease(ctx)
		return nil, fmt.Errorf("DuplicateOutput: 0x%08X", uint32(hr))
	}
	comRelease(output1)

	// IDXGIOutputDuplication::GetDesc (vtable idx 7)
	var desc dxgiOutduplDesc
	comCall(dupl, 7, uintptr(unsafe.Pointer(&desc)))
	width, height := desc.Width, desc.Height
	if width == 0 || height == 0 {
		width, height = 1920, 1080
	}
	screenW, screenH = width, height

	// Create staging texture
	stageDesc := d3d11Texture2DDesc{
		Width: width, Height: height,
		MipLevels: 1, ArraySize: 1,
		Format:         dxgiFormatB8G8R8A8Unorm,
		SampleCount:    1,
		Usage:          d3d11UsageStaging,
		CPUAccessFlags: d3d11CPUAccessRead,
	}
	var stage uintptr
	// ID3D11Device::CreateTexture2D (vtable idx 5)
	if hr = comCall(device, 5,
		uintptr(unsafe.Pointer(&stageDesc)), 0,
		uintptr(unsafe.Pointer(&stage)),
	); hr != 0 {
		comRelease(dupl)
		comRelease(device)
		comRelease(ctx)
		return nil, fmt.Errorf("CreateTexture2D: 0x%08X", uint32(hr))
	}

	return &DDA{device: device, ctx: ctx, dupl: dupl, stage: stage, width: width, height: height}, nil
}

// Capture acquires one frame. Returns (img, accumulatedFrames, err).
// accumulatedFrames==0 means only cursor/metadata changed — screen pixels unchanged.
// img is owned by DDA and must not be retained across the next Capture call.
func (d *DDA) Capture() (*image.RGBA, uint32, error) {
	var frameInfo dxgiOutduplFrameInfo
	var resource uintptr

	// IDXGIOutputDuplication::AcquireNextFrame (vtable idx 8), 100 ms timeout
	hr := comCall(d.dupl, 8,
		100,
		uintptr(unsafe.Pointer(&frameInfo)),
		uintptr(unsafe.Pointer(&resource)),
	)
	if hr != 0 {
		return nil, 0, fmt.Errorf("AcquireNextFrame: 0x%08X", uint32(hr))
	}
	defer func() {
		comRelease(resource)
		comCall(d.dupl, 14) // ReleaseFrame (vtable idx 14)
	}()

	// IDXGIResource → ID3D11Texture2D
	tex, err := comQueryInterface(resource, &iidID3D11Texture2D)
	if err != nil {
		return nil, 0, err
	}
	defer comRelease(tex)

	// ID3D11DeviceContext::CopyResource(staging, src) (vtable idx 47)
	comCall(d.ctx, 47, d.stage, tex)

	// ID3D11DeviceContext::Map (vtable idx 14)
	var mapped d3d11MappedSubresource
	if hr = comCall(d.ctx, 14,
		d.stage, 0, d3d11MapRead, 0,
		uintptr(unsafe.Pointer(&mapped)),
	); hr != 0 {
		return nil, 0, fmt.Errorf("Map: 0x%08X", uint32(hr))
	}
	defer comCall(d.ctx, 15, d.stage, 0) // Unmap (vtable idx 15)

	// Reuse the RGBA buffer to avoid a 8 MB allocation every frame
	if d.imgBuf == nil {
		d.imgBuf = image.NewRGBA(image.Rect(0, 0, int(d.width), int(d.height)))
	}
	img := d.imgBuf

	pitch := int(mapped.RowPitch)
	if pitch == 0 {
		return nil, 0, fmt.Errorf("Map returned zero RowPitch (GPU driver reset)")
	}
	w := int(d.width)
	src32 := unsafe.Slice((*uint32)(unsafe.Pointer(mapped.PData)), pitch/4*int(d.height))
	dst32 := unsafe.Slice((*uint32)(unsafe.Pointer(&img.Pix[0])), w*int(d.height))
	srcStride := pitch / 4
	// DXGI captures in BGRA. monitor 模式直接把 BGRA 喂给 FFmpeg（-pix_fmt bgra），逐行 copy 即可；
	// assist 模式（image/jpeg 编码）需要 RGBA，逐像素 shuffle。
	if outputBGRA {
		for y := range int(d.height) {
			copy(dst32[y*w:y*w+w], src32[y*srcStride:y*srcStride+w])
		}
	} else {
		for y := range int(d.height) {
			srcRow := src32[y*srcStride : y*srcStride+w]
			dstRow := dst32[y*w : y*w+w]
			for i, bgra := range srcRow {
				dstRow[i] = (bgra & 0xFF00FF00) | ((bgra >> 16) & 0xFF) | ((bgra & 0xFF) << 16)
			}
		}
	}
	return img, frameInfo.AccumulatedFrames, nil
}

func (d *DDA) Close() {
	comRelease(d.stage)
	comRelease(d.dupl)
	comRelease(d.ctx)
	comRelease(d.device)
	// 释放后立即清零，使 Close 幂等：二次 Close 时 comRelease 的 obj!=0 守卫会跳过，
	// 避免对已释放的 COM 指针再次解引用（use-after-free 崩溃）。
	d.stage, d.dupl, d.ctx, d.device = 0, 0, 0, 0
}

// logGPUInfo 通过 DXGI 查询并输出显卡型号、显存大小及厂商/设备 ID，便于远程排查硬件兼容问题。
func logGPUInfo() {
	var device, ctx uintptr
	hr, _, _ := procD3D11CreateDevice.Call(
		0, d3dDriverTypeHardware, 0, 0, 0, 0,
		d3d11SDKVersion,
		uintptr(unsafe.Pointer(&device)), 0,
		uintptr(unsafe.Pointer(&ctx)),
	)
	if hr != 0 {
		log.Printf("[GPU] D3D11CreateDevice 失败: 0x%08X（无独立 GPU 或驱动未安装）", uint32(hr))
		return
	}
	defer comRelease(device)
	defer comRelease(ctx)

	dxgiDev, err := comQueryInterface(device, &iidIDXGIDevice)
	if err != nil {
		log.Printf("[GPU] IDXGIDevice query 失败: %v", err)
		return
	}
	defer comRelease(dxgiDev)

	// IDXGIDevice::GetAdapter (vtable idx 7)
	var adapter uintptr
	if hr = comCall(dxgiDev, 7, uintptr(unsafe.Pointer(&adapter))); hr != 0 {
		log.Printf("[GPU] GetAdapter 失败: 0x%08X", uint32(hr))
		return
	}
	defer comRelease(adapter)

	// IDXGIAdapter::GetDesc (vtable idx 8)
	var desc dxgiAdapterDesc
	if hr = comCall(adapter, 8, uintptr(unsafe.Pointer(&desc))); hr != 0 {
		log.Printf("[GPU] GetDesc 失败: 0x%08X", uint32(hr))
		return
	}

	name := syscall.UTF16ToString(desc.Description[:])
	dedicatedMB := uint64(desc.DedicatedVideoMemory) / 1024 / 1024
	sharedMB := uint64(desc.SharedSystemMemory) / 1024 / 1024
	log.Printf("[GPU] 型号: %s", name)
	log.Printf("[GPU] 专用显存: %d MB | 共享内存: %d MB | VendorID: 0x%04X | DeviceID: 0x%04X",
		dedicatedMB, sharedMB, desc.VendorId, desc.DeviceId)
}

// ── GDI capture (fallback for VMs without GPU) ────────────────────────────────

type bitmapInfoHeader struct {
	BiSize          uint32
	BiWidth         int32
	BiHeight        int32
	BiPlanes        uint16
	BiBitCount      uint16
	BiCompression   uint32
	BiSizeImage     uint32
	BiXPelsPerMeter int32
	BiYPelsPerMeter int32
	BiClrUsed       uint32
	BiClrImportant  uint32
}

type GDICap struct {
	width    int32
	height   int32
	imgBuf   *image.RGBA
	prevHash uint64 // 上一帧采样哈希，用于跳过未变化帧
}

func newGDICap() (*GDICap, error) {
	w, _, _ := procGetSystemMetrics.Call(0) // SM_CXSCREEN
	h, _, _ := procGetSystemMetrics.Call(1) // SM_CYSCREEN
	log.Printf("GDI init: SM_CXSCREEN=%d SM_CYSCREEN=%d", w, h)
	if w == 0 || h == 0 {
		return nil, fmt.Errorf("GetSystemMetrics returned 0")
	}
	screenW, screenH = uint32(w), uint32(h)
	// prevHash 初值设为不可能的值，避免首帧全黑（hash=0）与默认 prevHash=0 冲突，导致返回空缓冲
	return &GDICap{width: int32(w), height: int32(h), prevHash: ^uint64(0)}, nil
}

func (g *GDICap) Capture() (*image.RGBA, bool, error) {
	hScreenDC, _, _ := procGetDC.Call(0)
	if hScreenDC == 0 {
		return nil, false, fmt.Errorf("GetDC(NULL) failed")
	}
	defer procReleaseDC.Call(0, hScreenDC)

	memDC, _, _ := procCreateCompatibleDC.Call(hScreenDC)
	if memDC == 0 {
		return nil, false, fmt.Errorf("CreateCompatibleDC failed")
	}
	defer procDeleteDC.Call(memDC)

	// 用 CreateDIBSection 代替 CreateCompatibleBitmap + GetDIBits
	// DIB section 直接暴露像素内存，BitBlt 后可直接读取，无需 GetDIBits
	// 在 Hyper-V 虚拟显卡驱动上 GetDIBits 不可靠，此方法更兼容
	bmi := bitmapInfoHeader{
		BiSize:        uint32(unsafe.Sizeof(bitmapInfoHeader{})),
		BiWidth:       g.width,
		BiHeight:      -g.height, // 负数 = top-down，像素顺序与 image.RGBA 一致
		BiPlanes:      1,
		BiBitCount:    32,
		BiCompression: 0, // BI_RGB
	}
	var bits uintptr
	hDIBmp, _, errNo := procCreateDIBSection.Call(
		hScreenDC,
		uintptr(unsafe.Pointer(&bmi)),
		0, // DIB_RGB_COLORS
		uintptr(unsafe.Pointer(&bits)),
		0, 0,
	)
	if hDIBmp == 0 {
		return nil, false, fmt.Errorf("CreateDIBSection(%dx%d) failed errno=%v", g.width, g.height, errNo)
	}
	defer procDeleteObject.Call(hDIBmp)

	oldObj, _, _ := procSelectObject.Call(memDC, hDIBmp)
	defer procSelectObject.Call(memDC, oldObj)

	const srccopy = 0x00CC0020
	r, _, errNo := procBitBlt.Call(memDC, 0, 0, uintptr(g.width), uintptr(g.height), hScreenDC, 0, 0, srccopy)
	if r == 0 {
		return nil, false, fmt.Errorf("BitBlt(%dx%d) failed errno=%v", g.width, g.height, errNo)
	}

	if g.imgBuf == nil {
		g.imgBuf = image.NewRGBA(image.Rect(0, 0, int(g.width), int(g.height)))
	}

	// 快速采样哈希：每隔 ~500 像素取一个像素，用于判断帧是否变化
	// 采样而非全量比较，避免额外 8MB 内存复制
	total := int(g.width) * int(g.height)
	src32 := unsafe.Slice((*uint32)(unsafe.Pointer(bits)), total)
	var hash uint64
	step := total / 512
	if step < 1 {
		step = 1
	}
	for i := 0; i < total; i += step {
		hash = hash*2654435761 + uint64(src32[i])
	}
	if hash == g.prevHash {
		// 画面未变化，跳过像素复制和 JPEG 编码
		return g.imgBuf, false, nil
	}
	g.prevHash = hash

	// Windows DIB 原生就是 BGRA。monitor 模式直接 copy 给 FFmpeg（-pix_fmt bgra）；
	// assist 模式 image/jpeg 按 RGBA 解读 *image.RGBA，必须 shuffle。
	dst32 := unsafe.Slice((*uint32)(unsafe.Pointer(&g.imgBuf.Pix[0])), total)
	if outputBGRA {
		copy(dst32, src32)
	} else {
		for i, bgra := range src32 {
			dst32[i] = (bgra & 0xFF00FF00) | ((bgra >> 16) & 0xFF) | ((bgra & 0xFF) << 16)
		}
	}
	return g.imgBuf, true, nil
}

// ── capturer interface (DDA or GDI) ──────────────────────────────────────────

type capturer interface {
	captureFrame() (img *image.RGBA, changed bool, err error)
	close()
}

type ddaCapt struct{ d *DDA }

func (c *ddaCapt) captureFrame() (*image.RGBA, bool, error) {
	img, acc, err := c.d.Capture()
	return img, acc > 0, err
}
func (c *ddaCapt) close() { c.d.Close() }

type gdiCapt struct{ g *GDICap }

func (c *gdiCapt) captureFrame() (*image.RGBA, bool, error) {
	return c.g.Capture()
}
func (c *gdiCapt) close() {}

// ── Win32 input injection ─────────────────────────────────────────────────────

// sendMouseEvent sends one mouse INPUT event via SendInput.
// INPUT layout on 64-bit (40 bytes): [type(4)][pad(4)][MOUSEINPUT(32)]
// MOUSEINPUT: [dx(4)][dy(4)][mouseData(4)][dwFlags(4)][time(4)][pad(4)][dwExtraInfo(8)]
func sendMouseEvent(flags uint32, dx, dy, mouseData int32) {
	var buf [40]byte
	*(*uint32)(unsafe.Pointer(&buf[0])) = inputTypeMouse
	*(*int32)(unsafe.Pointer(&buf[8])) = dx
	*(*int32)(unsafe.Pointer(&buf[12])) = dy
	*(*int32)(unsafe.Pointer(&buf[16])) = mouseData
	*(*uint32)(unsafe.Pointer(&buf[20])) = flags
	procSendInput.Call(1, uintptr(unsafe.Pointer(&buf[0])), 40)
}

// sendKeyEvent sends one keyboard INPUT event via SendInput.
// KEYBDINPUT at offset 8: [wVk(2)][wScan(2)][dwFlags(4)][time(4)][pad(4)][dwExtraInfo(8)]
func sendKeyEvent(vk uint16, keyup bool) {
	var buf [40]byte
	*(*uint32)(unsafe.Pointer(&buf[0])) = inputTypeKeyboard
	*(*uint16)(unsafe.Pointer(&buf[8])) = vk
	if keyup {
		*(*uint32)(unsafe.Pointer(&buf[12])) = keyEventfKeyUp
	}
	procSendInput.Call(1, uintptr(unsafe.Pointer(&buf[0])), 40)
}

// jsCodeToVK maps JS KeyboardEvent.code → Windows Virtual Key code.
var jsCodeToVK = map[string]uint16{
	"KeyA": 0x41, "KeyB": 0x42, "KeyC": 0x43, "KeyD": 0x44,
	"KeyE": 0x45, "KeyF": 0x46, "KeyG": 0x47, "KeyH": 0x48,
	"KeyI": 0x49, "KeyJ": 0x4A, "KeyK": 0x4B, "KeyL": 0x4C,
	"KeyM": 0x4D, "KeyN": 0x4E, "KeyO": 0x4F, "KeyP": 0x50,
	"KeyQ": 0x51, "KeyR": 0x52, "KeyS": 0x53, "KeyT": 0x54,
	"KeyU": 0x55, "KeyV": 0x56, "KeyW": 0x57, "KeyX": 0x58,
	"KeyY": 0x59, "KeyZ": 0x5A,
	"Digit0": 0x30, "Digit1": 0x31, "Digit2": 0x32, "Digit3": 0x33,
	"Digit4": 0x34, "Digit5": 0x35, "Digit6": 0x36, "Digit7": 0x37,
	"Digit8": 0x38, "Digit9": 0x39,
	"F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
	"F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
	"F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
	"Space": 0x20, "Enter": 0x0D, "Escape": 0x1B,
	"Backspace": 0x08, "Tab": 0x09, "Delete": 0x2E, "Insert": 0x2D,
	"Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
	"ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28,
	"ShiftLeft": 0xA0, "ShiftRight": 0xA1,
	"ControlLeft": 0xA2, "ControlRight": 0xA3,
	"AltLeft": 0xA4, "AltRight": 0xA5,
	"MetaLeft": 0x5B, "MetaRight": 0x5C,
	"CapsLock": 0x14, "NumLock": 0x90, "ScrollLock": 0x91,
	"Minus": 0xBD, "Equal": 0xBB, "BracketLeft": 0xDB, "BracketRight": 0xDD,
	"Backslash": 0xDC, "Semicolon": 0xBA, "Quote": 0xDE,
	"Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF, "Backquote": 0xC0,
	"Numpad0": 0x60, "Numpad1": 0x61, "Numpad2": 0x62, "Numpad3": 0x63,
	"Numpad4": 0x64, "Numpad5": 0x65, "Numpad6": 0x66, "Numpad7": 0x67,
	"Numpad8": 0x68, "Numpad9": 0x69,
	"NumpadMultiply": 0x6A, "NumpadAdd": 0x6B, "NumpadSubtract": 0x6D,
	"NumpadDecimal": 0x6E, "NumpadDivide": 0x6F, "NumpadEnter": 0x0D,
	"PrintScreen": 0x2C, "Pause": 0x13,
}

type controlMsg struct {
	Type   string  `json:"type"`
	X      int32   `json:"x"`
	Y      int32   `json:"y"`
	Button string  `json:"button"`
	Dy     float64 `json:"dy"`
	Code   string  `json:"code"`
}

var (
	ctrlRateMu     sync.Mutex
	ctrlRateCount  int
	ctrlRateReset  time.Time
	ctrlRateLimit  = 120
	ctrlRateWindow = time.Second
	auditFileMu    sync.Mutex // 保护 assist-audit.jsonl 的所有读写操作
	auditReportURL string
	authTokenVal   string
	serverURL      string // 中心服务器地址，供 collectLocalInfo 拨服务器选路
	localIP        string
	localMAC       string
	localHostname  string
	// monitor 模式直接把 DXGI/GDI 的原生 BGRA 数据喂给 FFmpeg（-pix_fmt bgra），
	// 跳过 BGRA→RGBA 逐像素 shuffle，节省 ~3-5% CPU。
	// assist 模式仍需 RGBA，因为 image/jpeg.Encode 按 RGBA 解读 *image.RGBA。
	outputBGRA bool
)

func checkCtrlRate() bool {
	ctrlRateMu.Lock()
	defer ctrlRateMu.Unlock()
	now := time.Now()
	if now.Sub(ctrlRateReset) > ctrlRateWindow {
		ctrlRateCount = 0
		ctrlRateReset = now
	}
	ctrlRateCount++
	return ctrlRateCount <= ctrlRateLimit
}

type assistAuditEntry struct {
	Event     string `json:"event"`
	Token     string `json:"token,omitempty"`
	RemoteIP  string `json:"remote_ip,omitempty"`
	Detail    string `json:"detail,omitempty"`
	Duration  int64  `json:"duration,omitempty"`
	IP        string `json:"ip,omitempty"`
	MAC       string `json:"mac,omitempty"`
	Hostname  string `json:"hostname,omitempty"`
	Timestamp string `json:"timestamp"`
	Reported  bool   `json:"reported"`
}

func writeAssistAudit(entry assistAuditEntry) {
	entry.Timestamp = time.Now().Format("2006-01-02T15:04:05.000Z07:00")
	if entry.IP == "" {
		entry.IP = localIP
	}
	if entry.MAC == "" {
		entry.MAC = localMAC
	}
	if entry.Hostname == "" {
		entry.Hostname = localHostname
	}
	entry.Reported = false
	appendAuditJsonl(entry)
	go func() {
		if reportAssistAuditSync(entry) {
			markAuditReported(entry.Timestamp)
		}
	}()
}

func reportAssistAuditSync(entry assistAuditEntry) bool {
	if auditReportURL == "" {
		log.Printf("[Audit] audit-url 未配置，跳过上报 event=%s", entry.Event)
		return false
	}
	payload := map[string]interface{}{
		"event":     entry.Event,
		"token":     entry.Token,
		"remote_ip": entry.RemoteIP,
		"detail":    entry.Detail,
		"ip":        entry.IP,
		"mac":       entry.MAC,
		"hostname":  entry.Hostname,
		"duration":  entry.Duration,
	}
	data, marshalErr := json.Marshal(payload)
	if marshalErr != nil {
		log.Printf("[Audit] JSON 序列化失败: %v event=%s", marshalErr, entry.Event)
		return false
	}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Post(auditReportURL, "application/json", bytes.NewReader(data))
	if err != nil {
		log.Printf("[Audit] POST %s 失败: %v event=%s", auditReportURL, err, entry.Event)
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		log.Printf("[Audit] POST %s 返回 %d: %s event=%s", auditReportURL, resp.StatusCode, string(body), entry.Event)
		return false
	}
	log.Printf("[Audit] 上报成功 event=%s url=%s", entry.Event, auditReportURL)
	return true
}

func getAuditJsonlPath() string {
	exePath, exeErr := os.Executable()
	if exeErr != nil {
		return ""
	}
	return filepath.Join(filepath.Dir(exePath), "assist-audit.jsonl")
}

func appendAuditJsonl(entry assistAuditEntry) {
	data, err := json.Marshal(entry)
	if err != nil {
		return
	}
	logPath := getAuditJsonlPath()
	if logPath == "" {
		return
	}
	auditFileMu.Lock()
	defer auditFileMu.Unlock()
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err != nil {
		return
	}
	defer f.Close()
	f.Write(append(data, '\n'))
}

func markAuditReported(timestamp string) {
	logPath := getAuditJsonlPath()
	if logPath == "" {
		return
	}
	auditFileMu.Lock()
	defer auditFileMu.Unlock()

	// 第一步：读取全部内容，然后立即关闭读句柄，
	// 避免写回时同一文件被两个句柄同时占用（Windows 下尤其重要）。
	f, err := os.Open(logPath)
	if err != nil {
		return
	}
	var kept [][]byte
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := append([]byte(nil), sc.Bytes()...)
		var entry struct {
			Timestamp string `json:"timestamp"`
			Reported  bool   `json:"reported"`
		}
		if json.Unmarshal(line, &entry) == nil && entry.Timestamp == timestamp && !entry.Reported {
			var full assistAuditEntry
			if json.Unmarshal(line, &full) == nil {
				full.Reported = true
				if updated, err := json.Marshal(full); err == nil {
					kept = append(kept, append(updated, '\n'))
					continue
				}
			}
		}
		kept = append(kept, append(line, '\n'))
	}
	f.Close() // 显式关闭，再写回

	// 第二步：写回
	var buf bytes.Buffer
	for _, l := range kept {
		buf.Write(l)
	}
	os.WriteFile(logPath, buf.Bytes(), 0644)
}

func replayUnreportedAudit() {
	logPath := getAuditJsonlPath()
	if logPath == "" {
		return
	}

	// 第一步：读取未上报条目后立即关闭文件句柄，
	// 避免后续 markAuditReported 写回时与本句柄并存。
	var unreported []assistAuditEntry
	func() {
		auditFileMu.Lock()
		defer auditFileMu.Unlock()
		f, err := os.Open(logPath)
		if err != nil {
			return
		}
		defer f.Close()
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
		for sc.Scan() {
			var entry assistAuditEntry
			if json.Unmarshal(sc.Bytes(), &entry) == nil && !entry.Reported {
				unreported = append(unreported, entry)
			}
		}
	}()

	// 第二步：逐条补报（markAuditReported 内部自己加锁）
	if len(unreported) == 0 {
		return
	}
	log.Printf("[Audit] 发现 %d 条未上报日志，开始补报", len(unreported))
	for _, entry := range unreported {
		if reportAssistAuditSync(entry) {
			markAuditReported(entry.Timestamp)
		} else {
			log.Printf("[Audit] 补报失败 event=%s timestamp=%s，停止补报", entry.Event, entry.Timestamp)
			return
		}
	}
	log.Printf("[Audit] 补报完成，共 %d 条", len(unreported))
}

// screenVirtualKeywords 按名称排除的虚拟网卡关键词（不含 vethernet，见 collectLocalInfo 注释）。
var screenVirtualKeywords = []string{
	"vmware", "vmnet", "virtual", "hyper-v", "loopback",
	"teredo", "isatap", "miniport", "tunnel", "pseudo",
	"wintun", "wireguard", "tap-", "tap0", "tun0",
	"nordlynx", "mullvad", "zerotier", "tailscale", "hamachi", "npcap",
}

// screenMatchesVirtualKeyword 判断网卡名（小写）是否命中已知虚拟/VPN 适配器关键词。
func screenMatchesVirtualKeyword(nameLower string) bool {
	for _, kw := range screenVirtualKeywords {
		if strings.Contains(nameLower, kw) {
			return true
		}
	}
	return false
}

// screenUDPRouteTarget 从 serverURL 解析出 UDP 选路目标 host:port。
// 拨向中心服务器选路，源 IP 即服务器能回连的 LAN 网卡。serverURL 为空时返回空字符串。
func screenUDPRouteTarget(serverURL string) string {
	u, err := url.Parse(serverURL)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	port := u.Port()
	if port == "" {
		port = "80"
	}
	return net.JoinHostPort(u.Hostname(), port)
}

// collectLocalInfo 获取本机首选物理/桥接网卡的 IP、MAC 和主机名。
// vEthernet 不按名字统一过滤——Hyper-V 外部交换机 "vEthernet (以太网)" 继承物理网卡 IP，
// 是真实 LAN 地址；仅按 IP 段 172.16.0.0/12 排除 NAT 网关（Default Switch / Docker / WSL）。
// 优先级：UDP 拨服务器选路 → 含"以太网"/"Ethernet" → 非 vEthernet 物理网卡 → 列表第一个。
func collectLocalInfo() {
	localHostname, _ = os.Hostname()
	ifs, err := net.Interfaces()
	if err != nil {
		return
	}

	// 优先：拨向中心服务器让系统路由自动选路，得到服务器能回连的 LAN 网卡。
	// 拨 8.8.8.8 会选公网默认路由网卡，在有旁路网卡的机器上可能取到非 LAN IP。
	if target := screenUDPRouteTarget(serverURL); target != "" {
		if conn, err := net.Dial("udp", target); err == nil {
			defer conn.Close()
			if udpAddr, ok := conn.LocalAddr().(*net.UDPAddr); ok {
				if ip4 := udpAddr.IP.To4(); ip4 != nil &&
					!(ip4[0] == 169 && ip4[1] == 254) &&
					!(ip4[0] == 198 && (ip4[1] == 18 || ip4[1] == 19)) &&
					!(ip4[0] == 100 && ip4[1] >= 64 && ip4[1] <= 127) {
					preferredIP := ip4.String()
					for _, iface := range ifs {
						if len(iface.HardwareAddr) == 0 {
							continue
						}
						nameLower := strings.ToLower(iface.Name)
						addrs, _ := iface.Addrs()
						for _, a := range addrs {
							ipNet, ok := a.(*net.IPNet)
							if !ok {
								continue
							}
							if ip4b := ipNet.IP.To4(); ip4b != nil && ip4b.String() == preferredIP {
								// 与 network-util.js 一致：选路命中虚拟/VPN 网卡则拒绝，回退物理网卡
								if screenMatchesVirtualKeyword(nameLower) {
									break
								}
								if strings.Contains(nameLower, "vethernet") &&
									ip4b[0] == 172 && ip4b[1] >= 16 && ip4b[1] <= 31 {
									break
								}
								localIP, localMAC = preferredIP, iface.HardwareAddr.String()
								return
							}
						}
					}
				}
			}
		}
	}

	type candidate struct {
		ip  string
		mac string
	}
	var candidates []candidate

	for _, iface := range ifs {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		nameLower := strings.ToLower(iface.Name)
		if screenMatchesVirtualKeyword(nameLower) {
			continue
		}
		isVEthernet := strings.Contains(nameLower, "vethernet")

		addrs, err := iface.Addrs()
		if err != nil || len(addrs) == 0 {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			ip4 := ipNet.IP.To4()
			if ip4 == nil {
				continue
			}
			// APIPA / RFC2544 / CGN
			if (ip4[0] == 169 && ip4[1] == 254) ||
				(ip4[0] == 198 && (ip4[1] == 18 || ip4[1] == 19)) ||
				(ip4[0] == 100 && ip4[1] >= 64 && ip4[1] <= 127) {
				continue
			}
			// vEthernet 额外排除 172.16.0.0/12（Hyper-V NAT / Docker / WSL 网关段）
			if isVEthernet && ip4[0] == 172 && ip4[1] >= 16 && ip4[1] <= 31 {
				continue
			}
			candidates = append(candidates, candidate{ip: ip4.String(), mac: iface.HardwareAddr.String()})
		}
	}

	if len(candidates) == 0 {
		return
	}

	// 优先级 1：含"以太网"或"ethernet"
	for i, iface := range ifs {
		_ = i
		n := iface.Name
		if strings.Contains(n, "以太网") || strings.EqualFold(safePrefix(n, 8), "ethernet") {
			for _, c := range candidates {
				if c.mac == iface.HardwareAddr.String() {
					localIP, localMAC = c.ip, c.mac
					return
				}
			}
		}
	}
	// 优先级 2：非 vEthernet
	for _, iface := range ifs {
		if strings.Contains(strings.ToLower(iface.Name), "vethernet") {
			continue
		}
		for _, c := range candidates {
			if c.mac == iface.HardwareAddr.String() {
				localIP, localMAC = c.ip, c.mac
				return
			}
		}
	}
	// 兜底
	localIP, localMAC = candidates[0].ip, candidates[0].mac
}

func safePrefix(s string, n int) string {
	if len(s) < n {
		return s
	}
	return s[:n]
}

func handleControl(data []byte) {
	if !checkCtrlRate() {
		return
	}
	var msg controlMsg
	if err := json.Unmarshal(data, &msg); err != nil {
		return
	}

	sw := int32(screenW)
	sh := int32(screenH)
	if sw == 0 {
		r, _, _ := procGetSystemMetrics.Call(0) // SM_CXSCREEN
		sw = int32(r)
	}
	if sh == 0 {
		r, _, _ := procGetSystemMetrics.Call(1) // SM_CYSCREEN
		sh = int32(r)
	}
	// 防止 captureLoop 还未完成初始化时 GetSystemMetrics 也失败导致除零 panic
	if sw == 0 || sh == 0 {
		return
	}

	switch msg.Type {
	case "mouse_move":
		absX := msg.X * 65535 / sw
		absY := msg.Y * 65535 / sh
		sendMouseEvent(meMove|meAbsolute, absX, absY, 0)

	case "mouse_down":
		var flags uint32
		switch msg.Button {
		case "right":
			flags = meRightDown
		case "middle":
			flags = meMiddleDown
		default:
			flags = meLeftDown
		}
		sendMouseEvent(flags, 0, 0, 0)

	case "mouse_up":
		var flags uint32
		switch msg.Button {
		case "right":
			flags = meRightUp
		case "middle":
			flags = meMiddleUp
		default:
			flags = meLeftUp
		}
		sendMouseEvent(flags, 0, 0, 0)

	case "wheel":
		// JS deltaY: positive = scroll down; Windows: positive = scroll up → negate
		delta := int32(-msg.Dy)
		if delta > 360 {
			delta = 360
		} else if delta < -360 {
			delta = -360
		}
		if delta != 0 {
			sendMouseEvent(meWheel, 0, 0, delta)
		}

	case "key_down":
		if vk, ok := jsCodeToVK[msg.Code]; ok {
			sendKeyEvent(vk, false)
		}

	case "key_up":
		if vk, ok := jsCodeToVK[msg.Code]; ok {
			sendKeyEvent(vk, true)
		}
	}
}

// ── WebSocket server + capture loop ──────────────────────────────────────────

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

const clientSendBuf = 512

type client struct {
	conn *websocket.Conn
	send chan []byte
}

func newClient(conn *websocket.Conn) *client {
	c := &client{conn: conn, send: make(chan []byte, clientSendBuf)}
	go c.writePump()
	return c
}

// writePump drains the send channel and writes frames to the WebSocket.
// Running in its own goroutine means a slow client never blocks the capture loop.
func (c *client) writePump() {
	for data := range c.send {
		if err := c.conn.WriteMessage(websocket.BinaryMessage, data); err != nil {
			break
		}
	}
	c.conn.Close()
}

func (c *client) close() {
	close(c.send)
}

// controlConn tracks the current exclusive /control WebSocket connection.
// Only one control connection is allowed at a time; a new one kicks out the old.
var (
	controlMu    sync.Mutex
	controlConn  *websocket.Conn
	controlToken string
	controlSince time.Time
)

func setControlConn(conn *websocket.Conn, token string) bool {
	controlMu.Lock()
	defer controlMu.Unlock()
	if controlConn != nil {
		return false
	}
	controlConn = conn
	controlToken = token
	controlSince = time.Now()
	log.Printf("[Control] New controller accepted from %s", conn.RemoteAddr())
	return true
}

func kickAndSetControlConn(conn *websocket.Conn, token string, newRemoteIP string) {
	controlMu.Lock()
	var old *websocket.Conn
	var oldSince time.Time
	var oldToken string
	if controlConn != nil {
		old = controlConn
		oldSince = controlSince
		oldToken = controlToken
		controlConn = nil
	}
	controlConn = conn
	controlToken = token
	controlSince = time.Now()
	controlMu.Unlock()

	if old != nil {
		duration := int64(time.Since(oldSince).Seconds())
		old.WriteMessage(websocket.TextMessage, []byte(`{"type":"kicked","reason":"new_controller"}`))
		old.Close()
		log.Printf("[Control] Kicked existing controller, accepting new connection from %s", conn.RemoteAddr())
		writeAssistAudit(assistAuditEntry{Event: "control_kicked", Token: oldToken, RemoteIP: newRemoteIP, Detail: "forced takeover", Duration: duration})
	}
}

func clearControlConn(conn *websocket.Conn) {
	controlMu.Lock()
	defer controlMu.Unlock()
	if controlConn == conn {
		controlConn = nil
		controlToken = ""
		controlSince = time.Time{}
		log.Printf("[Control] Controller disconnected from %s", conn.RemoteAddr())
	}
}

func getControlStatus() (bool, string, string) {
	controlMu.Lock()
	defer controlMu.Unlock()
	if controlConn == nil {
		return false, "", ""
	}
	return true, controlToken, controlSince.Format("2006-01-02 15:04:05")
}

type hub struct {
	mu        sync.Mutex
	clients   map[*client]bool
	initSeg   []byte
	initReady bool
}

func (h *hub) add(c *client) {
	h.mu.Lock()
	if h.initReady && len(h.initSeg) > 0 {
		select {
		case c.send <- h.initSeg:
		default:
			log.Printf("[Hub] Init segment send failed (buffer full), client will miss header")
		}
	}
	h.clients[c] = true
	h.mu.Unlock()
}

func (h *hub) remove(c *client) {
	h.mu.Lock()
	_, ok := h.clients[c]
	if ok {
		delete(h.clients, c)
	}
	h.mu.Unlock()
	if ok {
		c.close()
	}
}

func (h *hub) broadcast(data []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.initReady && len(data) >= 8 {
		hasFtyp := false
		hasMoov := false
		offset := 0
		for offset+8 <= len(data) {
			boxSize := int(binary.BigEndian.Uint32(data[offset : offset+4]))
			if boxSize < 8 || offset+boxSize > len(data) {
				break
			}
			boxType := string(data[offset+4 : offset+8])
			if boxType == "ftyp" {
				hasFtyp = true
			} else if boxType == "moov" {
				hasMoov = true
			}
			offset += boxSize
		}
		if hasFtyp && hasMoov {
			h.initSeg = make([]byte, len(data))
			copy(h.initSeg, data)
			h.initReady = true
			log.Printf("[Hub] fMP4 init segment (ftyp+moov) cached in single chunk (%d bytes)", len(h.initSeg))
		} else if hasFtyp {
			h.initSeg = make([]byte, len(data))
			copy(h.initSeg, data)
		} else if hasMoov {
			h.initSeg = append(h.initSeg, data...)
			h.initReady = true
			log.Printf("[Hub] fMP4 init segment (ftyp+moov) cached across chunks (%d bytes)", len(h.initSeg))
		} else if h.initSeg != nil {
			h.initSeg = nil
		}
	}
	for c := range h.clients {
		select {
		case c.send <- data:
		default:
			log.Printf("[Hub] Client send buffer full (%d), closing to prevent fMP4 stream corruption", len(c.send))
			c.conn.Close()
		}
	}
}

func (h *hub) count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.clients)
}

const logRetainHours = 48

func pruneOldLogLines(filePath string) {
	info, err := os.Stat(filePath)
	if err != nil || info.Size() == 0 {
		return
	}
	f, err := os.Open(filePath)
	if err != nil {
		return
	}
	defer f.Close()
	cutoff := time.Now().Add(-logRetainHours * time.Hour)
	var kept [][]byte
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(kept) > 0 {
			kept = append(kept, append([]byte(nil), line...))
			continue
		}
		lineStr := strings.TrimSpace(string(line))
		var t time.Time
		var parseErr error
		if len(lineStr) >= 23 {
			t, parseErr = time.ParseInLocation("2006/01/02 15:04:05.000", lineStr[:23], time.Local)
		}
		if parseErr != nil && len(lineStr) >= 23 {
			t, parseErr = time.ParseInLocation("2006-01-02 15:04:05.000", lineStr[:23], time.Local)
		}
		if parseErr == nil && t.After(cutoff) {
			kept = append(kept, append([]byte(nil), line...))
		}
	}
	if len(kept) == 0 {
		os.Truncate(filePath, 0)
		return
	}
	var buf bytes.Buffer
	for _, l := range kept {
		buf.Write(l)
		buf.WriteByte('\n')
	}
	os.WriteFile(filePath, buf.Bytes(), 0644)
}

func pruneOldJsonl(filePath string) {
	info, err := os.Stat(filePath)
	if err != nil || info.Size() == 0 {
		return
	}
	f, err := os.Open(filePath)
	if err != nil {
		return
	}
	defer f.Close()
	cutoff := time.Now().Add(-logRetainHours * time.Hour)
	var kept [][]byte
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var entry struct {
			Timestamp string `json:"timestamp"`
		}
		if json.Unmarshal(line, &entry) == nil && entry.Timestamp != "" {
			t, err := time.Parse(time.RFC3339, entry.Timestamp)
			if err == nil && t.After(cutoff) {
				kept = append(kept, append([]byte(nil), line...))
			}
		} else {
			kept = append(kept, append([]byte(nil), line...))
		}
	}
	if len(kept) == 0 {
		os.Truncate(filePath, 0)
		return
	}
	var buf bytes.Buffer
	for _, l := range kept {
		buf.Write(l)
		buf.WriteByte('\n')
	}
	os.WriteFile(filePath, buf.Bytes(), 0644)
}

func startLogPruneTimer() {
	ticker := time.NewTicker(1 * time.Hour)
	go func() {
		for range ticker.C {
			exePath, exeErr := os.Executable()
			if exeErr != nil {
				continue
			}
			exeDir := filepath.Dir(exePath)
			pruneOldLogLines(filepath.Join(exeDir, "screen-helper.log"))
			pruneOldJsonl(filepath.Join(exeDir, "assist-audit.jsonl"))
		}
	}()
}

// maskRTMPToken 把 RTMP URL 中的 token 查询参数打码，避免推流 token 写入日志。
//   "rtmp://h:1935/live/abc?token=xxx" → "rtmp://h:1935/live/abc?token=***"
func maskRTMPToken(rtmpURL string) string {
	i := strings.Index(rtmpURL, "token=")
	if i < 0 {
		return rtmpURL
	}
	end := strings.IndexByte(rtmpURL[i:], '&')
	if end < 0 {
		return rtmpURL[:i] + "token=***"
	}
	return rtmpURL[:i] + "token=***" + rtmpURL[i+end:]
}

func main() {
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(os.Stderr, "[screen-helper] PANIC: %v\n", r)
			os.Exit(1)
		}
	}()

	electronSpawned := flag.Bool("electron-spawned", false, "internal: must be true when spawned by Electron")
	bind := flag.String("bind", "127.0.0.1", "bind address")
	port := flag.Int("port", 19301, "WebSocket listen port (assist mode only)")
	quality := flag.Int("quality", 80, "JPEG quality 1-100")
	fps := flag.Int("fps", 15, "capture frame rate")
	scaleWidth := flag.Int("scale-width", 1280, "scale capture to this max width (0=disable)")
	mode := flag.String("mode", "monitor", "capture mode: monitor | assist")
	authToken := flag.String("auth-token", "", "token required for /control and /control-status")
	auditReportURLFlag := flag.String("audit-url", "", "URL to POST assist audit events (e.g. http://host:port/api/assist-audit-logs)")
	rtmpURL := flag.String("rtmp", "", "RTMP push URL, e.g. rtmp://srs-host:1935/live/AA:BB:CC:DD:EE:FF?token=...")
	serverURLFlag := flag.String("server-url", "", "中心服务器地址，用于拨服务器选路确定本机 LAN IP")
	flag.Parse()

	if !*electronSpawned {
		fmt.Fprintln(os.Stderr, "此程序由系统自动调用，不能直接运行。")
		os.Exit(1)
	}
	auditReportURL = *auditReportURLFlag
	authTokenVal = *authToken
	serverURL = *serverURLFlag
	// monitor 模式走 FFmpeg，可直接吃 BGRA；assist 模式走 image/jpeg，必须 RGBA
	outputBGRA = *mode != "assist"
	collectLocalInfo()

	var logPath string
	var logFile *os.File

	if exePath, exeErr := os.Executable(); exeErr == nil {
		exeDir := filepath.Dir(exePath)
		candidate := filepath.Join(exeDir, "screen-helper.log")
		if lf, lfErr := os.OpenFile(candidate, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644); lfErr == nil {
			logPath = candidate
			logFile = lf
		}
	}

	if logFile == nil {
		candidate := filepath.Join(os.TempDir(), "screen-helper.log")
		if lf, lfErr := os.OpenFile(candidate, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644); lfErr == nil {
			logPath = candidate
			logFile = lf
		}
	}

	if logFile != nil {
		log.SetOutput(io.MultiWriter(os.Stdout, logFile))
	} else {
		log.SetOutput(os.Stdout)
		logPath = "(none - log file could not be created)"
	}
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	if exePath, exeErr := os.Executable(); exeErr == nil {
		exeDir := filepath.Dir(exePath)
		pruneOldLogLines(filepath.Join(exeDir, "screen-helper.log"))
		pruneOldJsonl(filepath.Join(exeDir, "assist-audit.jsonl"))
	}
	startLogPruneTimer()

	go replayUnreportedAudit()

	log.Printf("screen-helper starting, log file: %s", logPath)
	fmt.Fprintf(os.Stdout, "[screen-helper] log file: %s\n", logPath)

	log.Printf("[SysInfo] hostname=%s ip=%s mac=%s mode=%s fps=%d",
		localHostname, localIP, localMAC, *mode, *fps)
	logGPUInfo()

	h := &hub{clients: make(map[*client]bool)}

	http.HandleFunc("/screen", func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("token")
		if authTokenVal != "" && token != authTokenVal {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		c := newClient(conn)
		h.add(c)
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				h.remove(c)
				return
			}
		}
	})

	http.HandleFunc("/control", func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("token")
		force := r.URL.Query().Get("force") == "1"
		if authTokenVal != "" && token != authTokenVal {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			log.Printf("[Control] Rejected connection from %s: invalid token", r.RemoteAddr)
			writeAssistAudit(assistAuditEntry{Event: "control_rejected", Token: token, RemoteIP: r.RemoteAddr, Detail: "invalid token"})
			return
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		if force {
			kickAndSetControlConn(conn, token, conn.RemoteAddr().String())
			writeAssistAudit(assistAuditEntry{Event: "control_takeover", Token: token, RemoteIP: conn.RemoteAddr().String(), Detail: "forced takeover"})
		} else {
			ok := setControlConn(conn, token)
			if !ok {
				conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"occupied","reason":"already_has_controller"}`))
				conn.Close()
				log.Printf("[Control] Rejected connection from %s: already occupied", r.RemoteAddr)
				writeAssistAudit(assistAuditEntry{Event: "control_rejected", Token: token, RemoteIP: r.RemoteAddr, Detail: "already occupied"})
				return
			}
			writeAssistAudit(assistAuditEntry{Event: "control_connected", Token: token, RemoteIP: conn.RemoteAddr().String()})
		}
		controlStart := time.Now()
		defer func() {
			clearControlConn(conn)
			writeAssistAudit(assistAuditEntry{Event: "control_disconnected", Token: token, RemoteIP: conn.RemoteAddr().String(), Duration: int64(time.Since(controlStart).Seconds())})
			conn.Close()
		}()
		for {
			_, msg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			handleControl(msg)
		}
	})

	http.HandleFunc("/control-status", func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("token")
		if authTokenVal != "" && token != authTokenVal {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Access-Control-Allow-Origin", "*")
		active, _, since := getControlStatus()
		json.NewEncoder(w).Encode(map[string]interface{}{"active": active, "since": since})
	})

	if *mode == "assist" {
		go captureLoop(h, *fps, *quality, *scaleWidth, true)
		addr := fmt.Sprintf("%s:%d", *bind, *port)
		// 先 Listen 再打日志，保证 JS 端 waitListening 看到 "listening" 时端口确实已经 bind
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("screen-helper listening on ws://%s/screen", addr)
		if err := http.Serve(ln, nil); err != nil {
			log.Fatal(err)
		}
	} else {
		// monitor 模式：纯 RTMP 推流，不绑定 WebSocket 端口
		go captureH264(h, *fps, *rtmpURL)
		log.Printf("screen-helper monitor listening, rtmp=%s", maskRTMPToken(*rtmpURL))
		select {} // block forever
	}
}

// scaleDown resizes src so its width is at most maxW, maintaining aspect ratio.
// Returns src unchanged if maxW==0 or src is already within bounds.
func scaleDown(src *image.RGBA, maxW int) *image.RGBA {
	if maxW <= 0 {
		return src
	}
	b := src.Bounds()
	srcW, srcH := b.Dx(), b.Dy()
	if srcW <= maxW {
		return src
	}
	dstW := maxW
	dstH := srcH * maxW / srcW
	dst := image.NewRGBA(image.Rect(0, 0, dstW, dstH))
	scaleX := float64(srcW) / float64(dstW)
	scaleY := float64(srcH) / float64(dstH)
	for y := 0; y < dstH; y++ {
		sy := int(float64(y)*scaleY + 0.5)
		if sy >= srcH {
			sy = srcH - 1
		}
		for x := 0; x < dstW; x++ {
			sx := int(float64(x)*scaleX + 0.5)
			if sx >= srcW {
				sx = srcW - 1
			}
			dst.SetRGBA(x, y, src.RGBAAt(sx, sy))
		}
	}
	return dst
}

// ── H.264 monitor mode ────────────────────────────────────────────────────────

// ffmpegPath returns the path to ffmpeg.exe placed alongside this binary.
func ffmpegPath() string {
	exe, _ := os.Executable()
	return filepath.Join(filepath.Dir(exe), "ffmpeg.exe")
}

var jobObjectHandle uintptr

func assignToJobObject(pid int) {
	if jobObjectHandle == 0 {
		k32 := syscall.NewLazyDLL("kernel32.dll")
		pCreateJO := k32.NewProc("CreateJobObjectW")
		pSetInfo := k32.NewProc("SetInformationJobObject")

		handle, _, _ := pCreateJO.Call(0, 0)
		if handle == 0 {
			log.Println("[JobObject] CreateJobObject failed")
			return
		}
		jobObjectHandle = handle

		type JOBOBJECT_BASIC_LIMIT_INFORMATION struct {
			PerProcessUserTimeLimit int64
			PerJobUserTimeLimit     int64
			LimitFlags              uint32
			MinimumWorkingSetSize   uintptr
			MaximumWorkingSetSize   uintptr
			ActiveProcessLimit      uint32
			Affinity                uintptr
			PriorityClass           uint32
			SchedulingClass         uint32
		}
		type JOBOBJECT_EXTENDED_LIMIT_INFORMATION struct {
			BasicLimitInformation JOBOBJECT_BASIC_LIMIT_INFORMATION
			IoInfo                [16]byte
			ProcessMemoryLimit    uintptr
			JobMemoryLimit        uintptr
			PeakProcessMemory     uintptr
			PeakJobMemory         uintptr
		}
		const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
		const JobObjectExtendedLimitInformation = 9

		var info JOBOBJECT_EXTENDED_LIMIT_INFORMATION
		info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		pSetInfo.Call(
			jobObjectHandle,
			uintptr(JobObjectExtendedLimitInformation),
			uintptr(unsafe.Pointer(&info)),
			uintptr(unsafe.Sizeof(info)),
		)
		log.Println("[JobObject] Created with KILL_ON_JOB_CLOSE")
	}

	k32 := syscall.NewLazyDLL("kernel32.dll")
	pOpenProc := k32.NewProc("OpenProcess")
	pAssign := k32.NewProc("AssignProcessToJobObject")
	pCloseH := k32.NewProc("CloseHandle")

	const PROCESS_SET_QUOTA = 0x0100
	const PROCESS_TERMINATE = 0x0001
	procHandle, _, _ := pOpenProc.Call(PROCESS_SET_QUOTA|PROCESS_TERMINATE, 0, uintptr(pid))
	if procHandle == 0 {
		log.Printf("[JobObject] OpenProcess(%d) failed", pid)
		return
	}
	ret, _, _ := pAssign.Call(jobObjectHandle, procHandle)
	if ret == 0 {
		log.Printf("[JobObject] AssignProcessToJobObject(%d) failed", pid)
	} else {
		log.Printf("[JobObject] PID %d assigned to job", pid)
	}
	pCloseH.Call(procHandle)
}

var cachedEncoder struct {
	sync.Once
	encoder string
	pixFmt  string
}

func selectEncoder(cap capturer) (encoder, pixFmt string) {
	cachedEncoder.Do(func() {
		// 直接吃 DXGI/GDI 的原生 BGRA，Go 端不做 BGRA→RGBA shuffle
		pixFmt = "bgra"
		encoders := []string{"h264_nvenc", "h264_qsv", "h264_amf"}
		for _, enc := range encoders {
			testArgs := []string{
				"-f", "lavfi", "-i", "nullsrc=s=320x240:d=0.1",
				"-c:v", enc, "-profile:v", "baseline", "-f", "null", "-",
				"-loglevel", "error",
			}
			cmd := exec.Command(ffmpegPath(), testArgs...)
			var errBuf bytes.Buffer
			cmd.Stderr = &errBuf
			if err := cmd.Run(); err == nil {
				log.Printf("[FFmpeg] Hardware encoder available: %s (baseline profile)", enc)
				cachedEncoder.encoder = enc
				cachedEncoder.pixFmt = pixFmt
				return
			} else {
				reason := strings.TrimSpace(errBuf.String())
				if reason == "" {
					reason = err.Error()
				}
				log.Printf("[FFmpeg] Encoder probe failed: %s — %s", enc, reason)
			}
		}
		log.Println("[FFmpeg] No hardware encoder found, falling back to libx264")
		cachedEncoder.encoder = "libx264"
		cachedEncoder.pixFmt = pixFmt
	})
	return cachedEncoder.encoder, cachedEncoder.pixFmt
}

// probeCapturer 做一次真实截图，验证 capturer 不只是"创建成功"而是"真能采集"。
// 锁屏 / RDP 未就绪时 newGDICap() 会创建成功但后续 BitBlt 全部失败，必须靠探测识别。
// DDA 首帧返回 WAIT_TIMEOUT 是正常的（屏幕尚无变化），不算失败。
func probeCapturer(c capturer) bool {
	if _, _, err := c.captureFrame(); err != nil {
		if _, ok := c.(*ddaCapt); ok {
			var code uint32
			fmt.Sscanf(err.Error(), "AcquireNextFrame: 0x%08X", &code)
			if code == dxgiErrorWaitTimeout {
				return true
			}
		}
		return false
	}
	return true
}

// recoverCapturer 在采集彻底失败后就地恢复一个可用的 capturer：优先 DDA，退回 GDI。
// 锁屏 / UAC 安全桌面 / RDP 断开 / 显示器休眠期间桌面不可采集，会带退避地一直重试，
// 直到桌面恢复——绝不退出进程。退出只会被 Electron 主进程重启后再次踩坑，
// 表现为前端画面反复转圈。调用方需在调用前自行关闭旧 capturer。
func recoverCapturer() capturer {
	backoff := time.Second
	for {
		if dda, err := newDDA(); err == nil {
			c := &ddaCapt{d: dda}
			if probeCapturer(c) {
				log.Println("[Screen] capturer 已恢复: DDA")
				return c
			}
			c.close()
		} else {
			log.Printf("[Screen] DDA 暂不可用: %v", err)
		}
		if gdi, err := newGDICap(); err == nil {
			c := &gdiCapt{g: gdi}
			if probeCapturer(c) {
				log.Println("[Screen] capturer 已恢复: GDI")
				return c
			}
			c.close()
		} else {
			log.Printf("[Screen] GDI 暂不可用: %v", err)
		}
		log.Printf("[Screen] 桌面当前不可采集（锁屏/RDP/休眠？），%v 后重试", backoff)
		time.Sleep(backoff)
		if backoff < 5*time.Second {
			backoff += time.Second
		}
	}
}

// ddaProvenWorking 跨会话记录本机 DDA 是否真正成功采集过帧。
// 一旦为 true，后续会话遇到持续超时即可确定是"画面静止"而非"DDA 不可用(VM/RDP)"——
// 真实硬件即便会话开局就对着静止画面，也不会被误判降级到 GDI。
// 仅 monitor 模式单 goroutine 读写，无需加锁。
var ddaProvenWorking bool

func captureH264(h *hub, fps int, rtmpURL string) {
	// origFps 保存调用方传入的目标帧率。下面 GDI 回退会把 fps 压到 8，
	// panic 重启时必须用 origFps，否则一旦降级过，重启后帧率永远回不到原值。
	origFps := fps
	// captureH264 跑在独立 goroutine 里，main() 的 recover() 捕获不到这里的 panic
	// （recover 只对同一 goroutine 生效）。COM/unsafe 解引用一旦 panic 会直接崩进程，
	// 故在此自带兜底：捕获后延迟重启采集，而不是让整个进程退出。
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[Screen] captureH264 panic 已恢复: %v —— 2s 后重启采集（不退出进程）", r)
			time.Sleep(2 * time.Second)
			go captureH264(h, origFps, rtmpURL)
		}
	}()
	var cap capturer
	dda, err := newDDA()
	if err != nil {
		log.Printf("[Screen] DDA init failed, falling back to GDI: %v", err)
		gdi, err2 := newGDICap()
		if err2 != nil {
			// 启动时桌面就不可采集（开机即锁屏 / RDP 未就绪），不退出进程，
			// 带退避重试直到桌面可用
			log.Printf("[Screen] 启动时 DDA/GDI 均不可用 (GDI:%v)，等待桌面可用...", err2)
			cap = recoverCapturer()
		} else {
			cap = &gdiCapt{g: gdi}
		}
		if fps > 8 {
			fps = 8
		}
	} else {
		cap = &ddaCapt{d: dda}
		log.Println("[Screen] Using DDA hardware capture")
	}
	defer cap.close()

	if testImg, _, testErr := cap.captureFrame(); testErr != nil {
		log.Printf("[Screen] STARTUP TEST CAPTURE FAILED: %v", testErr)
	} else {
		b := testImg.Bounds()
		log.Printf("[Screen] STARTUP TEST CAPTURE OK: %dx%d", b.Dx(), b.Dy())
	}

	interval := time.Second / time.Duration(fps)
	log.Printf("[Screen] H.264 monitor ready, target %d FPS", fps)

	for {
		if h.count() > 0 || rtmpURL != "" {
			log.Printf("[Screen] starting FFmpeg session (viewers=%d rtmp=%q)", h.count(), maskRTMPToken(rtmpURL))
			if err := runFFmpegSession(h, cap, fps, interval, rtmpURL); err != nil {
				log.Printf("[Screen] FFmpeg session ended: %v", err)
				if dc, ok := cap.(*ddaCapt); ok && err.Error() == "DDA access lost" {
					dc.d.Close()
					// 桌面会话过渡（锁屏/UAC/RDP 切换）可能持续数秒，单次 reinit 容易踩在过渡态失败
					// 三次递增退避（1.5s / 2.5s / 3.5s），都失败再回退 GDI
					var reinitErr error
					var newD *DDA
					backoffs := []time.Duration{1500 * time.Millisecond, 2500 * time.Millisecond, 3500 * time.Millisecond}
					for i, wait := range backoffs {
						time.Sleep(wait)
						newD, reinitErr = newDDA()
						if reinitErr != nil {
							log.Printf("[Screen] DDA reinit attempt %d/%d failed: %v", i+1, len(backoffs), reinitErr)
							continue
						}
						// sanity test：reinit 成功不代表能 capture，做一次真截图验证
						probe := &ddaCapt{d: newD}
						if _, _, probeErr := probe.captureFrame(); probeErr != nil {
							code := uint32(0)
							fmt.Sscanf(probeErr.Error(), "AcquireNextFrame: 0x%08X", &code)
							// timeout 是正常的（屏幕没变化），不算失败
							if code != dxgiErrorWaitTimeout {
								log.Printf("[Screen] DDA reinit attempt %d/%d capture probe failed: %v", i+1, len(backoffs), probeErr)
								newD.Close()
								newD = nil
								reinitErr = probeErr
								continue
							}
						}
						dc.d = newD
						log.Printf("[Screen] DDA reinitialized successfully on attempt %d/%d", i+1, len(backoffs))
						break
					}
					if reinitErr != nil {
						log.Println("[Screen] DDA reinit 失败，就地恢复 capturer（不退出进程）")
						cap = recoverCapturer()
					}
				} else if dc, ok := cap.(*ddaCapt); ok && err.Error() == "DXGI sustained timeout" {
					dc.d.Close()
					log.Println("[Screen] Switching to GDI due to DXGI sustained timeout (VM/RDP environment)")
					if gdi, gdiErr := newGDICap(); gdiErr == nil {
						cap = &gdiCapt{g: gdi}
						log.Println("[Screen] Switched to GDI capture successfully")
					} else {
						log.Printf("[Screen] GDI 也不可用 (%v)，就地恢复 capturer（不退出进程）", gdiErr)
						cap = recoverCapturer()
					}
				} else if _, ok := cap.(*gdiCapt); ok {
					log.Printf("[Screen] GDI 采集持续失败（桌面切换/锁屏致句柄失效？），就地恢复 capturer: %v", err)
					cap.close()
					cap = recoverCapturer()
				} else {
					time.Sleep(3 * time.Second)
				}
			}
			log.Println("[Screen] Session exited, returning to sentinel sleep")
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func runFFmpegSession(h *hub, cap capturer, fps int, interval time.Duration, rtmpURL string) error {
	if screenW == 0 || screenH == 0 {
		return fmt.Errorf("screen dimensions not initialised")
	}

	h.mu.Lock()
	h.initSeg = nil
	h.initReady = false
	h.mu.Unlock()

	encoder, pixFmt := selectEncoder(cap)

	args := []string{
		"-f", "rawvideo",
		"-vcodec", "rawvideo",
		"-pix_fmt", pixFmt,
		"-s", fmt.Sprintf("%dx%d", screenW, screenH),
		"-r", strconv.Itoa(fps),
		"-i", "pipe:0",
		// 540p：被控端普遍无硬件编码器、走 libx264 软编，缩到 960 宽可把编码像素量减约 44%
		"-vf", "scale=960:-2",
		"-c:v", encoder,
	}
	if encoder == "libx264" {
		// ultrafast 是 preset 阶梯里最省 CPU 的一档，软编场景优先吃 CPU 占用
		args = append(args, "-preset", "ultrafast", "-tune", "zerolatency")
	} else {
		args = append(args, "-profile:v", "baseline", "-bf", "0")
	}
	if rtmpURL != "" {
		// 直接推 FLV 到 RTMP，不再通过 tee 同时输出 fMP4 WebSocket
		args = append(args,
			"-b:v", "1000k",
			"-r", strconv.Itoa(fps),
			"-g", strconv.Itoa(fps),
			"-loglevel", "warning",
			"-f", "flv", rtmpURL,
		)
		log.Printf("[RTMP] pushing to %s", maskRTMPToken(rtmpURL))
	} else {
		args = append(args,
			"-b:v", "1000k",
			"-r", strconv.Itoa(fps),
			"-g", strconv.Itoa(fps),
			"-f", "mp4",
			"-movflags", "empty_moov+default_base_moof+frag_keyframe",
			"-loglevel", "error",
			"pipe:1",
		)
	}

	cmd := exec.Command(ffmpegPath(), args...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("stdout pipe: %w", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return fmt.Errorf("stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("ffmpeg start: %w", err)
	}
	log.Printf("[FFmpeg] started pid %d", cmd.Process.Pid)

	// 把 FFmpeg 的 stderr 逐行写入 screen-helper.log，便于诊断启动失败
	go func() {
		sc := bufio.NewScanner(stderr)
		sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
		for sc.Scan() {
			log.Printf("[FFmpeg stderr] %s", sc.Text())
		}
		if err := sc.Err(); err != nil {
			log.Printf("[FFmpeg stderr] scanner error: %v", err)
		}
	}()

	assignToJobObject(cmd.Process.Pid)

	done := make(chan struct{})
	go func() {
		defer close(done)
		if rtmpURL != "" {
			// RTMP 模式：FFmpeg 不写 stdout，静默排空防止管道阻塞
			io.Copy(io.Discard, stdout) //nolint:errcheck
			return
		}
		buf := make([]byte, 1<<16)
		for {
			n, err := stdout.Read(buf)
			if n > 0 {
				chunk := make([]byte, n)
				copy(chunk, buf[:n])
				h.broadcast(chunk)
			}
			if err != nil {
				break
			}
		}
	}()

	defer func() {
		stdin.Close()
		_ = cmd.Process.Kill()
		<-done // 等 stdout 读取 goroutine 退出
		// cmd.Wait() 回收子进程并释放进程句柄——不调用 Wait 会导致每个会话泄漏一个
		// 进程句柄。ffmpeg 的强制结束由上面的 Kill() 和 JobObject(KILL_ON_JOB_CLOSE)
		// 共同兜底，无需再按 PID taskkill（PID 可能已被其它进程复用，误杀风险）。
		_ = cmd.Wait()
		log.Println("[FFmpeg] process killed and reaped")
	}()

	consecFails := 0
	consecTimeouts := 0
	gotFrame := false // 本会话是否成功采集到过任何一帧
	idleMode := false // 是否已进入"静止空闲"状态（仅用于日志去重）
	var lastPix []byte

	for {
		start := time.Now()

		if h.count() == 0 && rtmpURL == "" {
			log.Println("[Screen] All viewers disconnected, closing H.264 session")
			return nil
		}

		img, _, captureErr := cap.captureFrame()
		if captureErr != nil {
			if _, ok := cap.(*ddaCapt); ok {
				code := uint32(0)
				fmt.Sscanf(captureErr.Error(), "AcquireNextFrame: 0x%08X", &code)
				if code == dxgiErrorWaitTimeout {
					consecTimeouts++
					// 100ms timeout × 100 = 10s 无任何画面变化。
					// 关键区分：若本会话从未采集到过帧，说明 DDA 在此环境根本拿不到画面
					// （VM/RDP/开机即锁屏），应回退 GDI；若曾经正常采集过，那只是用户对着
					// 静止画面（看文档/暂停视频）——真实硬件上很常见——继续 replay 上一帧
					// 维持推流即可，绝不能降级到 GDI（否则白白掉到 8fps 软编且断流重连）。
					if consecTimeouts >= 100 {
						if !gotFrame && !ddaProvenWorking {
							log.Println("[Screen] DXGI sustained timeout 且从未采集到帧，判定 VM/RDP，回退 GDI")
							return fmt.Errorf("DXGI sustained timeout")
						}
						if !idleMode {
							log.Println("[Screen] 画面静止 >10s（DDA 空闲），保持 DDA 并 replay 上一帧，不降级")
							idleMode = true
						}
						consecTimeouts = 0 // 重置，避免每帧重复判断/刷日志
					}
					consecFails = 0
					if lastPix != nil {
						if _, werr := stdin.Write(lastPix); werr != nil {
							return werr
						}
					}
					elapsed := time.Since(start)
					if elapsed < interval {
						time.Sleep(interval - elapsed)
					}
					continue
				}
				if code == dxgiErrorAccessLost || code == dxgiErrorInvalidCall {
					consecTimeouts = 0
					log.Printf("[Screen] DDA 复制对象失效 (0x%08X)，结束会话以重建", code)
					return fmt.Errorf("DDA access lost")
				}
				consecTimeouts = 0
				consecFails++
				if consecFails <= 3 {
					log.Printf("[Screen] DDA capture error (%d): %v", consecFails, captureErr)
				}
			} else {
				consecFails++
				if consecFails <= 3 {
					log.Printf("[Screen] GDI capture error (%d): %v", consecFails, captureErr)
				}
			}

			if consecFails >= 30 {
				return fmt.Errorf("capture failed %d consecutive times", consecFails)
			}
			elapsed := time.Since(start)
			if elapsed < interval {
				time.Sleep(interval - elapsed)
			}
			continue
		}

		consecFails = 0
		consecTimeouts = 0
		gotFrame = true
		idleMode = false
		if _, ok := cap.(*ddaCapt); ok {
			ddaProvenWorking = true // 本机 DDA 确实能出帧，后续会话不再误判 VM/RDP
		}
		if len(lastPix) != len(img.Pix) {
			lastPix = make([]byte, len(img.Pix))
		}
		copy(lastPix, img.Pix)

		if _, werr := stdin.Write(lastPix); werr != nil {
			return fmt.Errorf("stdin write: %w", werr)
		}

		elapsed := time.Since(start)
		if elapsed < interval {
			time.Sleep(interval - elapsed)
		}
	}
}

// ── JPEG assist mode ──────────────────────────────────────────────────────────

func captureLoop(h *hub, fps, quality, scaleWidth int, assistMode bool) {
	// 同 captureH264：本函数跑在独立 goroutine，main() 的 recover() 捕获不到这里的
	// panic，故自带兜底，捕获后延迟重启采集而非崩溃整个进程。
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[Screen] captureLoop panic 已恢复: %v —— 2s 后重启采集（不退出进程）", r)
			time.Sleep(2 * time.Second)
			go captureLoop(h, fps, quality, scaleWidth, assistMode)
		}
	}()
	var lastJPEG []byte

	// 优先尝试 DDA（GPU），VM 或无显卡时回退到 GDI
	var cap capturer
	dda, err := newDDA()
	if err != nil {
		log.Printf("DDA init failed (%v), falling back to GDI capture", err)
		gdi, err2 := newGDICap()
		if err2 != nil {
			// 启动时桌面不可采集，不退出进程，带退避重试直到桌面可用
			log.Printf("启动时 DDA/GDI 均不可用 (GDI:%v)，等待桌面可用...", err2)
			cap = recoverCapturer()
		} else {
			cap = &gdiCapt{g: gdi}
			log.Println("Using GDI capture (software mode)")
		}
		// 监控模式限制 GDI 帧率；远程协助模式不限制
		if !assistMode && fps > 8 {
			fps = 8
		}
	} else {
		cap = &ddaCapt{d: dda}
		log.Println("Using DDA capture (hardware mode)")
	}

	interval := time.Second / time.Duration(fps)
	log.Printf("capture interval: %v (%d fps)", interval, fps)
	defer cap.close()

	// 启动时强制执行一次截图，验证截图能力
	if testImg, _, testErr := cap.captureFrame(); testErr != nil {
		log.Printf("STARTUP TEST CAPTURE FAILED: %v", testErr)
	} else {
		b := testImg.Bounds()
		log.Printf("STARTUP TEST CAPTURE OK: %dx%d pixels", b.Dx(), b.Dy())
	}

	jopt := &jpeg.Options{Quality: quality}
	var jpegBuf bytes.Buffer

	// GDI 连续失败计数：屏幕锁定/显示器移除时连续失败超限则退出进程
	// 退出后前端收到 died 事件并进入等待，避免 WS 一直连着但收不到帧
	const maxConsecFails = 30 // ~3s at 10fps
	consecFails := 0
	consecTimeouts := 0

	// 静止帧心跳：屏幕无变化时每 2s 给新连接补发一帧，其余时间跳过广播节省 CPU
	const heartbeat = 2 * time.Second
	lastBroadcast := time.Time{}

	for {
		start := time.Now()

		if h.count() == 0 {
			time.Sleep(interval)
			continue
		}

		img, changed, err := cap.captureFrame()
		if err != nil {
			if dc, ok := cap.(*ddaCapt); ok {
				code := uint32(0)
				fmt.Sscanf(err.Error(), "AcquireNextFrame: 0x%08X", &code)
				if code == dxgiErrorWaitTimeout {
					consecTimeouts++
					// 100ms × 100 = 10s 无帧才认为是 VM/RDP 环境（正常空闲屏幕远低于此）
					if consecTimeouts >= 100 {
						log.Println("[Screen] DXGI sustained timeout (10s no frames), forcing GDI fallback")
						gdi, gdiErr := newGDICap()
						if gdiErr == nil {
							dc.d.Close()
							cap = &gdiCapt{g: gdi}
							consecFails = 0
						} else {
							consecFails++
						}
						// 无论成功失败都重置 timeout 计数器，避免每帧重复尝试创建 GDI
						consecTimeouts = 0
					} else {
						consecFails = 0
					}
				} else if code == dxgiErrorAccessLost {
					consecTimeouts = 0
					log.Println("DDA access lost, reinitialising...")
					time.Sleep(500 * time.Millisecond)
					dc.d.Close()
					newD, reinitErr := newDDA()
					if reinitErr != nil {
						log.Printf("DDA reinit failed (%v), switching to GDI", reinitErr)
						gdi, gdiErr := newGDICap()
						if gdiErr == nil {
							cap = &gdiCapt{g: gdi}
							consecFails = 0
						} else {
							time.Sleep(2 * time.Second)
						}
					} else {
						dc.d = newD
						consecFails = 0
					}
				} else {
					consecTimeouts = 0
					consecFails++
				}
			} else {
				consecFails++
				if consecFails <= 3 {
					log.Printf("GDI capture error (%d): %v", consecFails, err)
				}
			}
			if consecFails >= maxConsecFails {
				log.Printf("[Screen] 连续采集失败 %d 次，就地恢复 capturer（不退出进程）", consecFails)
				cap.close()
				cap = recoverCapturer()
				consecFails = 0
				consecTimeouts = 0
				continue
			}
			// assist 模式：timeout 时重播上一帧维持帧率流畅；monitor 模式：仅心跳
			if assistMode {
				if lastJPEG != nil {
					h.broadcast(lastJPEG)
				}
			} else if lastJPEG != nil && time.Since(lastBroadcast) >= heartbeat {
				h.broadcast(lastJPEG)
				lastBroadcast = time.Now()
			}
			elapsed := time.Since(start)
			if elapsed < interval {
				time.Sleep(interval - elapsed)
			}
			continue
		}

		consecFails = 0
		consecTimeouts = 0

		// assist 模式：每帧都编码广播（包括 changed=false 的光标移动帧）
		// monitor 模式：仅在变化时编码，静止帧靠心跳维持
		if changed || assistMode {
			jpegBuf.Reset()
			if err := jpeg.Encode(&jpegBuf, scaleDown(img, scaleWidth), jopt); err == nil {
				data := make([]byte, jpegBuf.Len())
				copy(data, jpegBuf.Bytes())
				lastJPEG = data
			}
		}
		if assistMode {
			if lastJPEG != nil {
				h.broadcast(lastJPEG)
			}
		} else if changed {
			if lastJPEG != nil {
				h.broadcast(lastJPEG)
				lastBroadcast = time.Now()
			}
		} else if lastJPEG != nil && time.Since(lastBroadcast) >= heartbeat {
			h.broadcast(lastJPEG)
			lastBroadcast = time.Now()
		}

		if elapsed := time.Since(start); elapsed < interval {
			time.Sleep(interval - elapsed)
		}
	}
}
