-- 客户端字符集：官方镜像入口默认 latin1 导入，中文会被二次编码（页面显示乱码）
SET NAMES utf8mb4;

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

-- 交互记录表 chat_message 已迁移至 PostgreSQL（月分区大表 + 与记忆同库）：
--   见 init/pg_memory_schema.sql 的 message.chat_message，MySQL 侧不再保留。
