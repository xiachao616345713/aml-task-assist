-- 既有配置记录的新字段留 NULL，继续使用环境变量默认值，直到从页面保存。
ALTER TABLE app_settings
    ADD COLUMN aml_base_url VARCHAR(2048) NULL,
    ADD COLUMN scheduler_aml_token TEXT NULL,
    ADD COLUMN auto_submit_enabled BOOLEAN NULL,
    ADD COLUMN auto_submit_interval_seconds BIGINT NULL,
    ADD COLUMN max_auto_submit_per_run BIGINT NULL;
