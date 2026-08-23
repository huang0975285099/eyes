//go:build windows

package web

func diskUsage(path string) (total, free uint64) {
	// Windows 上暂不支持磁盘统计，部署在 Linux Docker 中使用
	return 0, 0
}
