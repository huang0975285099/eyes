package database

import (
	"fmt"
	"log"
	"media-service/config"
	"media-service/models"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

var DB *gorm.DB

const (
	connectAttempts = 30
	connectInterval = 2 * time.Second
)

// Init waits for MySQL, configures the connection pool, and creates or updates
// every table owned by MediaService.
func Init(cfg *config.Config) error {
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%d)/%s?charset=%s&parseTime=True&loc=Local&timeout=5s&readTimeout=10s&writeTimeout=10s",
		cfg.Database.User,
		cfg.Database.Password,
		cfg.Database.Host,
		cfg.Database.Port,
		cfg.Database.DBName,
		cfg.Database.Charset,
	)

	var db *gorm.DB
	var lastErr error
	for attempt := 1; attempt <= connectAttempts; attempt++ {
		db, lastErr = gorm.Open(mysql.Open(dsn), &gorm.Config{
			Logger:      logger.Default.LogMode(logger.Warn),
			PrepareStmt: true,
		})
		if lastErr == nil {
			sqlDB, poolErr := db.DB()
			lastErr = poolErr
			if lastErr == nil {
				lastErr = sqlDB.Ping()
				if lastErr != nil {
					_ = sqlDB.Close()
				}
			}
		}
		if lastErr == nil {
			break
		}
		if attempt < connectAttempts {
			log.Printf("[database] MySQL 尚未就绪（%d/%d）: %v", attempt, connectAttempts, lastErr)
			time.Sleep(connectInterval)
		}
	}
	if lastErr != nil {
		return fmt.Errorf("连接 MySQL %s:%d/%s 失败: %w", cfg.Database.Host, cfg.Database.Port, cfg.Database.DBName, lastErr)
	}

	sqlDB, err := db.DB()
	if err != nil {
		return fmt.Errorf("获取数据库连接池失败: %w", err)
	}
	sqlDB.SetMaxOpenConns(30)
	sqlDB.SetMaxIdleConns(10)
	sqlDB.SetConnMaxLifetime(time.Hour)
	sqlDB.SetConnMaxIdleTime(10 * time.Minute)

	if err := db.AutoMigrate(
		&models.User{},
		&models.Computer{},
		&models.VideoSource{},
		&models.RecordingSetting{},
		&models.RecordingSegment{},
		&models.RecordingFrame{},
		&models.AIAlgorithm{},
		&models.VideoAnalysisRule{},
		&models.AIJob{},
		&models.AIWorker{},
		&models.AIEvent{},
	); err != nil {
		_ = sqlDB.Close()
		return fmt.Errorf("迁移 eyes 数据库表结构失败: %w", err)
	}

	DB = db
	log.Printf("[database] 已连接 MySQL %s:%d/%s，表结构迁移完成", cfg.Database.Host, cfg.Database.Port, cfg.Database.DBName)
	return nil
}

func Close() error {
	if DB == nil {
		return nil
	}
	sqlDB, err := DB.DB()
	if err != nil {
		return err
	}
	return sqlDB.Close()
}
