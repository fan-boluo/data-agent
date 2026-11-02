-- 初始化数据插入

-- 初始化用户admin 密码boluo123
INSERT INTO dataagent.`user`
(id, userAccount, userPassword, userName, userProfile, userRole, editTime, createTime, updateTime, isDelete)
VALUES('user_0001', 'admin', '3cf658b8932b950cb5cfe71bd1be582d', '管理员', '系统管理员', 'admin', now(), now(), now(), 0);