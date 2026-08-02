// Package nodeconfig 提供节点 Zone 配置的运行时共享状态。
// RecorderManager 与 Web Server 共享同一个 *ZoneConfig 实例，
// 后台修改车间分配后调用 Update 即可热更新，无需重启服务。
package nodeconfig

import (
	"strings"
	"sync"
)

// ZoneConfig 持有当前节点录制的 Zone ID 列表，支持并发安全读写。
type ZoneConfig struct {
	mu          sync.RWMutex
	nodeZoneIDs []uint
}

// New 创建 ZoneConfig，ids 为初始的 Zone ID 列表。
func New(ids []uint) *ZoneConfig {
	return &ZoneConfig{nodeZoneIDs: append([]uint(nil), ids...)}
}

// Get 返回当前 Zone ID 列表的副本。
func (z *ZoneConfig) Get() []uint {
	z.mu.RLock()
	defer z.mu.RUnlock()
	return append([]uint(nil), z.nodeZoneIDs...)
}

// Update 运行时更新 Zone ID 列表。
func (z *ZoneConfig) Update(ids []uint) {
	z.mu.Lock()
	defer z.mu.Unlock()
	z.nodeZoneIDs = append([]uint(nil), ids...)
}

// IsAllZones 返回 true 表示录制所有车间（配置为空、含 0）。
func (z *ZoneConfig) IsAllZones() bool {
	z.mu.RLock()
	defer z.mu.RUnlock()
	if len(z.nodeZoneIDs) == 0 {
		return true
	}
	for _, id := range z.nodeZoneIDs {
		if id == 0 {
			return true
		}
	}
	return false
}

// ZoneAllowed 返回 true 表示该 Zone 在配置的录制范围内。
// 一次性加锁，避免先调 IsAllZones 再加锁产生的 TOCTOU 竞态。
func (z *ZoneConfig) ZoneAllowed(zoneID uint) bool {
	z.mu.RLock()
	defer z.mu.RUnlock()
	if len(z.nodeZoneIDs) == 0 {
		return true
	}
	for _, id := range z.nodeZoneIDs {
		if id == 0 || id == zoneID {
			return true
		}
	}
	return false
}

// ZoneCond 返回 SQL WHERE 条件片段和参数，用于按 NodeZoneIDs 过滤。
// 全 Zone 模式时返回空字符串（不过滤）。一次性加锁，避免竞态。
func (z *ZoneConfig) ZoneCond() (string, []interface{}) {
	z.mu.RLock()
	defer z.mu.RUnlock()
	if len(z.nodeZoneIDs) == 0 {
		return "", nil
	}
	for _, id := range z.nodeZoneIDs {
		if id == 0 {
			return "", nil
		}
	}
	placeholders := make([]string, len(z.nodeZoneIDs))
	args := make([]interface{}, len(z.nodeZoneIDs))
	for i, id := range z.nodeZoneIDs {
		placeholders[i] = "?"
		args[i] = id
	}
	return "node_zone_id IN (" + strings.Join(placeholders, ",") + ")", args
}
