-- 创建库
create database if not exists dataagent;

-- 切换库
use dataagent;

-- 以下是建表语句

-- 用户表
create table if not exists user
(
    id           varchar(32)                            not null comment 'id（user_ + 4位随机，由服务层生成）' primary key,
    userAccount  varchar(256)                           not null comment '账号',
    userPassword varchar(512)                           not null comment '密码',
    userName     varchar(256)                           null comment '用户昵称',
    userProfile  varchar(512)                           null comment '用户简介',
    userRole     varchar(256) default 'user'            not null comment '用户角色：user/admin',
    editTime     datetime     default CURRENT_TIMESTAMP not null comment '编辑时间',
    createTime   datetime     default CURRENT_TIMESTAMP not null comment '创建时间',
    updateTime   datetime     default CURRENT_TIMESTAMP not null on update CURRENT_TIMESTAMP comment '更新时间',
    isDelete     tinyint      default 0                 not null comment '是否删除',
    UNIQUE KEY uk_userAccount (userAccount),
    INDEX idx_userName (userName)
) comment '用户' collate = utf8mb4_unicode_ci;

-- 应用表（项目）
create table if not exists app
(
    id           varchar(32)                            not null comment 'id（app_ + 4位随机，由服务层生成）' primary key,
    appName      varchar(128)                           not null comment '应用名称',
    dbName       varchar(128)                           null comment '项目关联的数据库名（一个项目一个库）',
    owner       varchar(32)                            not null comment '创建用户id（项目属主，user_xxxx）',
    createTime   datetime     default CURRENT_TIMESTAMP not null comment '创建时间',
    updateTime   datetime     default CURRENT_TIMESTAMP not null on update CURRENT_TIMESTAMP comment '更新时间',
    isDelete     tinyint      default 0                 not null comment '是否删除',
    INDEX idx_appName (appName),         -- 提升基于应用名称的查询性能
    INDEX idx_owner (owner)            -- 提升基于用户 ID 的查询性能
) comment '应用' collate = utf8mb4_unicode_ci;

-- 项目成员表（用户-项目多对多权限关系；项目属主即 app.owner，不在此表存 owner 记录）
create table if not exists app_member
(
    id         bigint auto_increment comment 'id' primary key,
    appId      varchar(32)                           not null comment '项目id（app_xxxx）',
    userId     varchar(32)                           not null comment '成员用户id（user_xxxx）',
    createTime datetime    default CURRENT_TIMESTAMP not null comment '创建时间',
    updateTime datetime    default CURRENT_TIMESTAMP not null on update CURRENT_TIMESTAMP comment '更新时间',
    isDelete   tinyint     default 0                 not null comment '是否删除',
    UNIQUE KEY uk_app_user (appId, userId), -- 一个用户在一个项目中只有一条成员记录
    INDEX idx_userId (userId)               -- 加速查询"我参与的项目列表"
) comment '项目成员' collate = utf8mb4_unicode_ci;

-- 交互记录表
CREATE TABLE if not exists `chat_message` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `message_uid`       CHAR(36)        NOT NULL,
  `user_id`           VARCHAR(32)     NOT NULL COMMENT '用户id（user_xxxx）',
  `session_id`        CHAR(36)        NOT NULL,
  `app_id`            VARCHAR(64)     NOT NULL,
  `seq`               BIGINT UNSIGNED NOT NULL COMMENT '会话内单调递增,提炼位点的载体',
  `role`              VARCHAR(16)     NOT NULL COMMENT 'user/assistant/tool',
  `content`           MEDIUMTEXT      NULL COMMENT '整条消息的 JSON 序列化（含 tool_calls 等结构字段）',
  `payload_ref`       VARCHAR(512)    NULL COMMENT '超长内容外置对象存储的 key',
  `content_bytes`     INT UNSIGNED    NOT NULL DEFAULT 0,
  `content_tokens`    INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '本条内容 token 数，全 role 有值；提炼触发与窗口预算用',
  `model`             VARCHAR(64)     NULL,
  `prompt_tokens`     INT UNSIGNED    NULL,
  `completion_tokens` INT UNSIGNED    NULL,
  `finish_reason`     VARCHAR(32)     NULL,
  `created_at`        DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_message_uid` (`message_uid`),
  UNIQUE KEY `uk_session_seq` (`session_id`, `seq`),
  KEY `idx_extract_scan` (`user_id`, `session_id`, `seq`),
  KEY `idx_user_created` (`user_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;