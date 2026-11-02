-- 初始化数据插入

-- 初始化用户
INSERT INTO dagent.`user`
(id, userAccount, userPassword, userName, userProfile, userRole, editTime, createTime, updateTime, isDelete)
VALUES('user_0001', 'admin', '10670d38ec32fa8102be6a37f8cb52bf', '管理员', '系统管理员', 'admin', now(), now(), now(), 0);