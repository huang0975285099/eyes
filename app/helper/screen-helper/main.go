//go:build windows

package main

import (
	"bufio"
	"bytes"
	"errors"
	"flag"
	"fmt"
	"image"
	"io"
	"log"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
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

func shouldReplayCachedDDA(hr uintptr, hasCachedFrame bool) bool {
	return hasCachedFrame && uint32(hr) == dxgiErrorWaitTimeout
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
		// 已成功采集过画面后，WAIT_TIMEOUT 只表示桌面静止。直接返回缓存帧，
		// 确保新建/重连的 FFmpeg 会话立刻获得首帧；从未采集成功时仍返回
		// timeout，让上层保留 VM/RDP 环境的 GDI 回退判断。
		if shouldReplayCachedDDA(hr, d.imgBuf != nil) {
			return d.imgBuf, 0, nil
		}
		return nil, 0, fmt.Errorf("AcquireNextFrame: 0x%08X", uint32(hr))
	}
	defer func() {
		comRelease(resource)
		comCall(d.dupl, 14) // ReleaseFrame (vtable idx 14)
	}()
	// Desktop Duplication 可能只报告鼠标指针/元数据变化。监控推流不合成
	// DXGI 指针形状；已有缓存帧时直接复用，避免无意义的整屏 GPU 回读和内存复制。
	if frameInfo.AccumulatedFrames == 0 && d.imgBuf != nil {
		return d.imgBuf, 0, nil
	}

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
	// DXGI captures in BGRA; FFmpeg consumes it directly with -pix_fmt bgra.
	for y := range int(d.height) {
		copy(dst32[y*w:y*w+w], src32[y*srcStride:y*srcStride+w])
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
	width     int32
	height    int32
	hScreenDC uintptr
	memDC     uintptr
	hDIBmp    uintptr
	oldObj    uintptr
	bits      uintptr
	imgBuf    *image.RGBA
}

var errGDIDisplayChanged = errors.New("GDI display dimensions changed")

func newGDICap() (*GDICap, error) {
	w, _, _ := procGetSystemMetrics.Call(0) // SM_CXSCREEN
	h, _, _ := procGetSystemMetrics.Call(1) // SM_CYSCREEN
	log.Printf("GDI init: SM_CXSCREEN=%d SM_CYSCREEN=%d", w, h)
	if w == 0 || h == 0 {
		return nil, fmt.Errorf("GetSystemMetrics returned 0")
	}
	hScreenDC, _, _ := procGetDC.Call(0)
	if hScreenDC == 0 {
		return nil, fmt.Errorf("GetDC(NULL) failed")
	}
	memDC, _, _ := procCreateCompatibleDC.Call(hScreenDC)
	if memDC == 0 {
		procReleaseDC.Call(0, hScreenDC)
		return nil, fmt.Errorf("CreateCompatibleDC failed")
	}
	bmi := bitmapInfoHeader{
		BiSize:        uint32(unsafe.Sizeof(bitmapInfoHeader{})),
		BiWidth:       int32(w),
		BiHeight:      -int32(h), // 负数 = top-down，像素顺序与 image.RGBA 一致
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
	if hDIBmp == 0 || bits == 0 {
		if hDIBmp != 0 {
			procDeleteObject.Call(hDIBmp)
		}
		procDeleteDC.Call(memDC)
		procReleaseDC.Call(0, hScreenDC)
		return nil, fmt.Errorf("CreateDIBSection(%dx%d) failed errno=%v", w, h, errNo)
	}
	oldObj, _, _ := procSelectObject.Call(memDC, hDIBmp)
	if oldObj == 0 || oldObj == ^uintptr(0) {
		procDeleteObject.Call(hDIBmp)
		procDeleteDC.Call(memDC)
		procReleaseDC.Call(0, hScreenDC)
		return nil, fmt.Errorf("SelectObject(DIB) failed")
	}
	screenW, screenH = uint32(w), uint32(h)
	return &GDICap{
		width: int32(w), height: int32(h),
		hScreenDC: hScreenDC, memDC: memDC, hDIBmp: hDIBmp, oldObj: oldObj, bits: bits,
		imgBuf: image.NewRGBA(image.Rect(0, 0, int(w), int(h))),
	}, nil
}

func (g *GDICap) Capture() (*image.RGBA, bool, error) {
	if g.hScreenDC == 0 || g.memDC == 0 || g.hDIBmp == 0 || g.bits == 0 {
		return nil, false, fmt.Errorf("GDI capture resources are closed")
	}
	currentW, _, _ := procGetSystemMetrics.Call(0) // SM_CXSCREEN
	currentH, _, _ := procGetSystemMetrics.Call(1) // SM_CYSCREEN
	if int32(currentW) != g.width || int32(currentH) != g.height {
		return nil, false, errGDIDisplayChanged
	}
	const srccopy = 0x00CC0020
	r, _, errNo := procBitBlt.Call(g.memDC, 0, 0, uintptr(g.width), uintptr(g.height), g.hScreenDC, 0, 0, srccopy)
	if r == 0 {
		return nil, false, fmt.Errorf("BitBlt(%dx%d) failed errno=%v", g.width, g.height, errNo)
	}

	// GDI 没有可靠的脏区信息。始终复制完整帧，避免稀疏采样遗漏文字、
	// 状态图标等小范围变化。GDI 会话已限制为最多 8 FPS。
	total := int(g.width) * int(g.height)
	src32 := unsafe.Slice((*uint32)(unsafe.Pointer(g.bits)), total)
	// Windows DIB 原生就是 BGRA，直接 copy 给 FFmpeg（-pix_fmt bgra）。
	dst32 := unsafe.Slice((*uint32)(unsafe.Pointer(&g.imgBuf.Pix[0])), total)
	copy(dst32, src32)
	return g.imgBuf, true, nil
}

func (g *GDICap) Close() {
	if g.memDC != 0 && g.oldObj != 0 {
		procSelectObject.Call(g.memDC, g.oldObj)
	}
	if g.hDIBmp != 0 {
		procDeleteObject.Call(g.hDIBmp)
	}
	if g.memDC != 0 {
		procDeleteDC.Call(g.memDC)
	}
	if g.hScreenDC != 0 {
		procReleaseDC.Call(0, g.hScreenDC)
	}
	g.hScreenDC, g.memDC, g.hDIBmp, g.oldObj, g.bits = 0, 0, 0, 0, 0
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
func (c *gdiCapt) close() { c.g.Close() }

var (
	serverURL     string // 中心服务器地址，供 collectLocalInfo 拨服务器选路
	localIP       string
	localMAC      string
	localHostname string
)

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
		}
	}()
}

// maskRTMPToken 把 RTMP URL 中的 token 查询参数打码，避免推流 token 写入日志。
//
//	"rtmp://h:1935/live/abc?token=xxx" → "rtmp://h:1935/live/abc?token=***"
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
	fps := flag.Int("fps", 15, "capture frame rate")
	rtmpURL := flag.String("rtmp", "", "RTMP push URL, e.g. rtmp://srs-host:1935/live/stream-name")
	serverURLFlag := flag.String("server-url", "", "中心服务器地址，用于拨服务器选路确定本机 LAN IP")
	flag.Parse()

	if !*electronSpawned {
		fmt.Fprintln(os.Stderr, "此程序由系统自动调用，不能直接运行。")
		os.Exit(1)
	}
	if *fps < 1 || *fps > 30 {
		fmt.Fprintln(os.Stderr, "fps 必须在 1 到 30 之间。")
		os.Exit(1)
	}
	if strings.TrimSpace(*rtmpURL) == "" {
		fmt.Fprintln(os.Stderr, "缺少 RTMP 推流地址。")
		os.Exit(1)
	}
	serverURL = *serverURLFlag
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
	}
	startLogPruneTimer()

	log.Printf("screen-helper starting, log file: %s", logPath)
	fmt.Fprintf(os.Stdout, "[screen-helper] log file: %s\n", logPath)

	log.Printf("[SysInfo] hostname=%s ip=%s mac=%s fps=%d",
		localHostname, localIP, localMAC, *fps)
	logGPUInfo()

	go captureH264(*fps, *rtmpURL)
	log.Printf("screen-helper RTMP monitor started, rtmp=%s", maskRTMPToken(*rtmpURL))
	select {} // block forever
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

func selectEncoder(width, height uint32) (encoder, pixFmt string) {
	// 使用实际桌面分辨率和 BGRA 输入路径探测，避免 320x240 探测成功、实际
	// 2K/4K 会话却不受硬件支持而反复启动失败。每个新推流会话都重新探测，
	// 这样驱动重置或 GPU 临时不可用后不会继续使用陈旧的成功缓存。
	pixFmt = "bgra"
	source := fmt.Sprintf("nullsrc=s=%dx%d:d=0.1", width, height)
	encoders := []string{"h264_nvenc", "h264_qsv", "h264_amf"}
	for _, enc := range encoders {
		testArgs := []string{
			"-hide_banner", "-nostdin",
			"-f", "lavfi", "-i", source,
			"-vf", "format=bgra",
			"-frames:v", "1",
			"-c:v", enc, "-profile:v", "baseline", "-bf", "0",
			"-loglevel", "error", "-f", "null", "-",
		}
		cmd := exec.Command(ffmpegPath(), testArgs...)
		cmd.Stdout = io.Discard
		var errBuf bytes.Buffer
		cmd.Stderr = &errBuf
		if err := cmd.Run(); err == nil {
			log.Printf("[FFmpeg] Hardware encoder available at %dx%d: %s", width, height, enc)
			return enc, pixFmt
		} else {
			reason := strings.TrimSpace(errBuf.String())
			if reason == "" {
				reason = err.Error()
			}
			log.Printf("[FFmpeg] Encoder probe failed at %dx%d: %s — %s", width, height, enc, reason)
		}
	}
	log.Printf("[FFmpeg] No hardware encoder available at %dx%d, falling back to libx264", width, height)
	return "libx264", pixFmt
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

func captureH264(fps int, rtmpURL string) {
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
			go captureH264(origFps, rtmpURL)
		}
	}()
	// D3D11/DXGI 上下文和长期复用的 GDI DC 都固定在同一 Windows 线程，
	// 同时满足 GetDC/ReleaseDC 的线程配对要求。
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
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
	} else {
		cap = &ddaCapt{d: dda}
		log.Println("[Screen] Using DDA hardware capture")
	}
	// cap 会在运行期间于 DDA/GDI 之间切换，退出时必须关闭当前对象，而不是
	// defer 注册瞬间的旧接口值。
	defer func() {
		if cap != nil {
			cap.close()
		}
	}()

	if testImg, _, testErr := cap.captureFrame(); testErr != nil {
		log.Printf("[Screen] STARTUP TEST CAPTURE FAILED: %v", testErr)
	} else {
		if _, usingDDA := cap.(*ddaCapt); usingDDA {
			ddaProvenWorking = true
		}
		b := testImg.Bounds()
		log.Printf("[Screen] STARTUP TEST CAPTURE OK: %dx%d", b.Dx(), b.Dy())
	}

	lastSessionFPS := 0
	for {
		sessionFPS := origFps
		if _, usingGDI := cap.(*gdiCapt); usingGDI && sessionFPS > 8 {
			sessionFPS = 8
		}
		interval := time.Second / time.Duration(sessionFPS)
		if sessionFPS != lastSessionFPS {
			log.Printf("[Screen] H.264 monitor target %d FPS", sessionFPS)
			lastSessionFPS = sessionFPS
		}
		if rtmpURL != "" {
			log.Printf("[Screen] starting FFmpeg session, rtmp=%q", maskRTMPToken(rtmpURL))
			if err := runFFmpegSession(cap, sessionFPS, interval, rtmpURL); err != nil {
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
			log.Println("[Screen] Session exited, retrying")
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func buildFFmpegArgs(encoder, pixFmt string, width, height uint32, fps int, rtmpURL string) (args []string, softwareScaled bool) {
	// 硬件编码器按显示器原始分辨率 100% 推流；libx264 软编码在宽度超过
	// 1280 时等比缩小，以控制没有硬件编码能力的客户端 CPU 占用。
	outputW, outputH := width, height
	softwareScaled = encoder == "libx264" && width > 1280
	if softwareScaled {
		outputW = 1280
		outputH = height * outputW / width
	}
	// 码率按实际编码输出的像素数分档，避免高分辨率画质不足，也避免软编缩小后
	// 仍沿用原始 2K/4K 档位而浪费带宽。
	bitrateKbps := 1800
	pixels := outputW * outputH
	switch {
	case pixels >= 3840*2160:
		bitrateKbps = 6000
	case pixels >= 2560*1440:
		bitrateKbps = 4000
	case pixels >= 1920*1080:
		bitrateKbps = 2500
	}
	bitrate := fmt.Sprintf("%dk", bitrateKbps)

	args = []string{
		"-f", "rawvideo",
		"-vcodec", "rawvideo",
		"-pix_fmt", pixFmt,
		"-s", fmt.Sprintf("%dx%d", width, height),
		"-r", strconv.Itoa(fps),
		"-i", "pipe:0",
	}
	if softwareScaled {
		args = append(args, "-vf", "scale=1280:-2")
	}
	args = append(args, "-c:v", encoder)
	if encoder == "libx264" {
		// ultrafast 是 preset 阶梯里最省 CPU 的一档，软编场景优先吃 CPU 占用
		args = append(args, "-preset", "ultrafast", "-tune", "zerolatency")
	} else {
		args = append(args, "-profile:v", "baseline", "-bf", "0")
	}
	args = append(args,
		"-b:v", bitrate,
		"-r", strconv.Itoa(fps),
		"-g", strconv.Itoa(fps),
		"-loglevel", "warning",
		"-f", "flv", rtmpURL,
	)
	return args, softwareScaled
}

func runFFmpegSession(cap capturer, fps int, interval time.Duration, rtmpURL string) error {
	if screenW == 0 || screenH == 0 {
		return fmt.Errorf("screen dimensions not initialised")
	}

	encoder, pixFmt := selectEncoder(screenW, screenH)
	args, softwareScaled := buildFFmpegArgs(encoder, pixFmt, screenW, screenH, fps, rtmpURL)
	if softwareScaled {
		log.Printf("[FFmpeg] libx264 software fallback: scaling %dx%d to 1280px width", screenW, screenH)
	}
	log.Printf("[RTMP] pushing to %s", maskRTMPToken(rtmpURL))

	cmd := exec.Command(ffmpegPath(), args...)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("stdin pipe: %w", err)
	}
	cmd.Stdout = io.Discard
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

	defer func() {
		stdin.Close()
		_ = cmd.Process.Kill()
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

		img, _, captureErr := cap.captureFrame()
		if captureErr != nil {
			if errors.Is(captureErr, errGDIDisplayChanged) {
				log.Println("[Screen] GDI display dimensions changed, rebuilding capture resources")
				return captureErr
			}
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
		// capturer 复用同一像素缓冲且本循环串行执行；直接保留切片引用即可。
		// timeout 时缓冲不会被改写，因此无需每帧额外复制一遍完整屏幕。
		lastPix = img.Pix
		if _, werr := stdin.Write(lastPix); werr != nil {
			return fmt.Errorf("stdin write: %w", werr)
		}

		elapsed := time.Since(start)
		if elapsed < interval {
			time.Sleep(interval - elapsed)
		}
	}
}
