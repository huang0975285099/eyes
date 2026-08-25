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
		&models.Customer{},
		&models.User{},
		&models.UserSession{},
		&models.VideoSource{},
		&models.VideoRecordingRule{},
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
	// Retention used to be stored in whole days. Preserve the configured
	// duration exactly when moving to hour-based rules, then remove the old
	// column so subsequent starts cannot overwrite newly saved hour values.
	if db.Migrator().HasColumn(&models.VideoRecordingRule{}, "retain_days") {
		if err := db.Exec("UPDATE video_recording_rules SET retain_hours = retain_days * 24").Error; err != nil {
			_ = sqlDB.Close()
			return fmt.Errorf("迁移录像保留时间到小时失败: %w", err)
		}
		if err := db.Migrator().DropColumn(&models.VideoRecordingRule{}, "retain_days"); err != nil {
			_ = sqlDB.Close()
			return fmt.Errorf("删除旧录像保留天数字段失败: %w", err)
		}
	}
	// The product no longer has a global recording switch. This is a clean
	// replacement rather than a compatibility layer, so remove its obsolete
	// table after the per-source rule table has been created successfully.
	if db.Migrator().HasTable("recording_settings") {
		if err := db.Migrator().DropTable("recording_settings"); err != nil {
			_ = sqlDB.Close()
			return fmt.Errorf("删除旧全局录像配置表失败: %w", err)
		}
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
