package database

import (
	"fmt"
	"log"
	"recording-service/config"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

var DB *gorm.DB

func Init(cfg *config.Config) {
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%d)/%s?charset=%s&parseTime=True&loc=Local",
		cfg.Database.User,
		cfg.Database.Password,
		cfg.Database.Host,
		cfg.Database.Port,
		cfg.Database.DBName,
		cfg.Database.Charset,
	)

	db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Warn),
	})
	if err != nil {
		log.Fatalf("[database] 连接失败: %v", err)
	}

	// 表结构由 A 节点 ukeysystem 的 AutoMigrate 统一管理，B 节点子账号无 DDL 权限，此处不迁移。

	DB = db
	log.Println("[database] 连接成功")
}
