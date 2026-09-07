-- MySQL dump 10.13  Distrib 5.7.40, for Linux (x86_64)
--
-- Host: localhost    Database: runnergo
-- ------------------------------------------------------
-- Server version	5.7.40

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `auto_plan`
--

DROP TABLE IF EXISTS `auto_plan`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `auto_plan` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键',
  `plan_id` varchar(100) NOT NULL COMMENT '计划ID',
  `rank_id` bigint(10) NOT NULL DEFAULT '0' COMMENT '序号ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `plan_name` varchar(255) NOT NULL COMMENT '计划名称',
  `task_type` tinyint(2) NOT NULL DEFAULT '1' COMMENT '计划类型：1-普通任务，2-定时任务',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '计划状：1-未开始，2-进行中',
  `create_user_id` varchar(100) NOT NULL COMMENT '创建人id',
  `run_user_id` varchar(100) NOT NULL COMMENT '运行人id',
  `remark` text COMMENT '备注',
  `run_count` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '运行次数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动化测试-计划表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auto_plan`
--

LOCK TABLES `auto_plan` WRITE;
/*!40000 ALTER TABLE `auto_plan` DISABLE KEYS */;
/*!40000 ALTER TABLE `auto_plan` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auto_plan_email`
--

DROP TABLE IF EXISTS `auto_plan_email`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `auto_plan_email` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `email` varchar(255) NOT NULL COMMENT '邮箱',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动化测计划—收件人邮箱表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auto_plan_email`
--

LOCK TABLES `auto_plan_email` WRITE;
/*!40000 ALTER TABLE `auto_plan_email` DISABLE KEYS */;
/*!40000 ALTER TABLE `auto_plan_email` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auto_plan_report`
--

DROP TABLE IF EXISTS `auto_plan_report`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `auto_plan_report` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `report_id` varchar(100) NOT NULL COMMENT '报告ID',
  `report_name` varchar(125) NOT NULL COMMENT '报告名称',
  `plan_id` varchar(100) NOT NULL COMMENT '计划ID',
  `rank_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '序号ID',
  `plan_name` varchar(255) NOT NULL COMMENT '计划名称',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `task_type` int(11) NOT NULL DEFAULT '0' COMMENT '任务类型',
  `task_mode` int(11) NOT NULL DEFAULT '0' COMMENT '运行模式：1-按测试用例运行',
  `control_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '控制模式：0-集中模式，1-单独模式',
  `scene_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '场景运行次序：1-顺序执行，2-同时执行',
  `test_case_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '测试用例运行次序：1-顺序执行，2-同时执行',
  `run_duration_time` bigint(20) NOT NULL DEFAULT '0' COMMENT '任务运行持续时长',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '报告状态1:进行中，2:已完成',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '启动人id',
  `remark` text NOT NULL COMMENT '备注',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间（执行时间）',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_report_id` (`report_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动化测试计划-报告表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auto_plan_report`
--

LOCK TABLES `auto_plan_report` WRITE;
/*!40000 ALTER TABLE `auto_plan_report` DISABLE KEYS */;
/*!40000 ALTER TABLE `auto_plan_report` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auto_plan_task_conf`
--

DROP TABLE IF EXISTS `auto_plan_task_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `auto_plan_task_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '配置ID',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `task_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '任务类型：1-普通模式，2-定时任务',
  `task_mode` tinyint(2) NOT NULL DEFAULT '1' COMMENT '运行模式：1-按照用例执行',
  `scene_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '场景运行次序：1-顺序执行，2-同时执行',
  `test_case_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '用例运行次序：1-顺序执行，2-同时执行',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人用户ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动化测试—普通任务配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auto_plan_task_conf`
--

LOCK TABLES `auto_plan_task_conf` WRITE;
/*!40000 ALTER TABLE `auto_plan_task_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `auto_plan_task_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `auto_plan_timed_task_conf`
--

DROP TABLE IF EXISTS `auto_plan_timed_task_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `auto_plan_timed_task_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '表id',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `frequency` int(10) unsigned NOT NULL DEFAULT '0' COMMENT '任务执行频次: 0-一次，1-每天，2-每周，3-每月',
  `task_exec_time` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '任务执行时间',
  `task_close_time` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '任务结束时间',
  `task_type` tinyint(2) NOT NULL DEFAULT '2' COMMENT '任务类型：1-普通任务，2-定时任务',
  `task_mode` tinyint(2) NOT NULL DEFAULT '1' COMMENT '运行模式：1-按照用例执行',
  `scene_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '场景运行次序：1-顺序执行，2-同时执行',
  `test_case_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '测试用例运行次序：1-顺序执行，2-同时执行',
  `status` tinyint(2) NOT NULL DEFAULT '0' COMMENT '任务状态：0-未启用，1-运行中，2-已过期',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人用户ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动化测试-定时任务配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `auto_plan_timed_task_conf`
--

LOCK TABLES `auto_plan_timed_task_conf` WRITE;
/*!40000 ALTER TABLE `auto_plan_timed_task_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `auto_plan_timed_task_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `company`
--

DROP TABLE IF EXISTS `company`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `company` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `company_id` varchar(100) NOT NULL COMMENT '企业id',
  `name` varchar(100) NOT NULL DEFAULT '' COMMENT '企业名称',
  `logo` varchar(255) NOT NULL DEFAULT '' COMMENT '企业logo',
  `expire_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '服务到期时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_company_id` (`company_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='企业表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `company`
--

LOCK TABLES `company` WRITE;
/*!40000 ALTER TABLE `company` DISABLE KEYS */;
INSERT INTO `company` VALUES (1,'12e1c2a9-5637-48af-ad20-00e286fa241f','runnergo','','2026-08-29 08:40:28','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL);
/*!40000 ALTER TABLE `company` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `element`
--

DROP TABLE IF EXISTS `element`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `element` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `element_id` varchar(100) NOT NULL COMMENT '全局唯一ID',
  `element_type` varchar(10) NOT NULL COMMENT '类型：文件夹，元素',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `name` varchar(255) NOT NULL COMMENT '名称',
  `parent_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '父级ID',
  `locators` json DEFAULT NULL COMMENT '定位元素属性',
  `sort` int(11) NOT NULL DEFAULT '0' COMMENT '排序',
  `version` int(11) NOT NULL DEFAULT '0' COMMENT '产品版本号',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人ID',
  `description` text NOT NULL COMMENT '备注',
  `source` tinyint(4) NOT NULL DEFAULT '0' COMMENT '数据来源：0-元素管理，1-场景管理',
  `source_id` varchar(100) NOT NULL DEFAULT '' COMMENT '引用来源ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_element_id` (`element_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='元素表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `element`
--

LOCK TABLES `element` WRITE;
/*!40000 ALTER TABLE `element` DISABLE KEYS */;
/*!40000 ALTER TABLE `element` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `global_variable`
--

DROP TABLE IF EXISTS `global_variable`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `global_variable` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '变量类型：1-全局变量，2-场景变量',
  `var` varchar(255) NOT NULL COMMENT '变量名',
  `val` text NOT NULL COMMENT '变量值',
  `description` text NOT NULL COMMENT '描述',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '开关状态：1-开启，2-关闭',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全局变量表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `global_variable`
--

LOCK TABLES `global_variable` WRITE;
/*!40000 ALTER TABLE `global_variable` DISABLE KEYS */;
/*!40000 ALTER TABLE `global_variable` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `machine`
--

DROP TABLE IF EXISTS `machine`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `machine` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `region` varchar(64) NOT NULL COMMENT '所属区域',
  `ip` varchar(16) NOT NULL COMMENT '机器IP',
  `port` int(11) unsigned NOT NULL COMMENT '端口',
  `name` varchar(200) NOT NULL COMMENT '机器名称',
  `cpu_usage` float unsigned NOT NULL DEFAULT '0' COMMENT 'CPU使用率',
  `cpu_load_one` float unsigned NOT NULL DEFAULT '0' COMMENT 'CPU-1分钟内平均负载',
  `cpu_load_five` float unsigned NOT NULL DEFAULT '0' COMMENT 'CPU-5分钟内平均负载',
  `cpu_load_fifteen` float unsigned NOT NULL DEFAULT '0' COMMENT 'CPU-15分钟内平均负载',
  `mem_usage` float unsigned NOT NULL DEFAULT '0' COMMENT '内存使用率',
  `disk_usage` float unsigned NOT NULL DEFAULT '0' COMMENT '磁盘使用率',
  `max_goroutines` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '最大协程数',
  `current_goroutines` bigint(20) NOT NULL DEFAULT '0' COMMENT '已用协程数',
  `server_type` tinyint(2) unsigned NOT NULL DEFAULT '1' COMMENT '机器类型：1-主力机器，2-备用机器',
  `status` tinyint(2) unsigned NOT NULL DEFAULT '1' COMMENT '机器状态：1-使用中，2-已卸载',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `machine_region_ip_status_index` (`region`,`ip`,`status`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='压力测试机器表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `machine`
--

LOCK TABLES `machine` WRITE;
/*!40000 ALTER TABLE `machine` DISABLE KEYS */;
INSERT INTO `machine` VALUES (1,'北京','172.18.0.13',30000,'b8bb8b039fd0',1.33556,0.54,0.52,0.58,24.8911,0,20005,0,1,1,'2026-08-29 08:40:28','2026-08-29 09:53:04',NULL);
/*!40000 ALTER TABLE `machine` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `migrations`
--

DROP TABLE IF EXISTS `migrations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `migrations` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `version` varchar(50) NOT NULL COMMENT '版本号',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=7 DEFAULT CHARSET=utf8mb4;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `migrations`
--

LOCK TABLES `migrations` WRITE;
/*!40000 ALTER TABLE `migrations` DISABLE KEYS */;
INSERT INTO `migrations` VALUES (1,'2.2.0','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(2,'2.1.0','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(3,'2.0.0.2','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(4,'2.0.0.1','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(5,'2.0.0','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(6,'1.1.2','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL);
/*!40000 ALTER TABLE `migrations` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `mock_target`
--

DROP TABLE IF EXISTS `mock_target`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `mock_target` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `target_id` varchar(100) NOT NULL COMMENT '全局唯一ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `target_type` varchar(10) NOT NULL COMMENT '类型：文件夹，接口，分组，场景,测试用例',
  `name` varchar(255) NOT NULL COMMENT '名称',
  `parent_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '父级ID',
  `method` varchar(16) NOT NULL COMMENT '方法',
  `sort` int(11) NOT NULL DEFAULT '0' COMMENT '排序',
  `type_sort` int(11) NOT NULL DEFAULT '0' COMMENT '类型排序',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '回收站状态：1-正常，2-回收站',
  `version` int(11) NOT NULL DEFAULT '0' COMMENT '产品版本号',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人ID',
  `recent_user_id` varchar(100) NOT NULL COMMENT '最近修改人ID',
  `description` text NOT NULL COMMENT '备注',
  `source` tinyint(4) NOT NULL DEFAULT '0' COMMENT '数据来源：0-mock管理',
  `plan_id` varchar(100) NOT NULL COMMENT '计划id',
  `source_id` varchar(100) NOT NULL COMMENT '引用来源ID',
  `is_checked` tinyint(2) NOT NULL DEFAULT '1' COMMENT '是否开启：1-开启，2-关闭',
  `is_disabled` tinyint(2) NOT NULL DEFAULT '0' COMMENT '运行计划时是否禁用：0-不禁用，1-禁用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_target_id` (`target_id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='创建目标';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `mock_target`
--

LOCK TABLES `mock_target` WRITE;
/*!40000 ALTER TABLE `mock_target` DISABLE KEYS */;
/*!40000 ALTER TABLE `mock_target` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `permission`
--

DROP TABLE IF EXISTS `permission`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `permission` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `permission_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '权限ID',
  `title` varchar(100) NOT NULL DEFAULT '' COMMENT '权限内容',
  `mark` varchar(100) NOT NULL DEFAULT '' COMMENT '权限标识',
  `url` varchar(100) NOT NULL DEFAULT '' COMMENT '权限url',
  `type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '类型（1：权限   2：功能）',
  `group_id` int(11) NOT NULL DEFAULT '0' COMMENT '所属权限组（1：企业成员管理  2：团队管理  3：角色管理）',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=22 DEFAULT CHARSET=utf8mb4 COMMENT='权限表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `permission`
--

LOCK TABLES `permission` WRITE;
/*!40000 ALTER TABLE `permission` DISABLE KEYS */;
INSERT INTO `permission` VALUES (1,101,'创建成员','company_save_member','/permission/api/v1/company/member/save',1,1,'2023-05-22 10:31:54','2023-05-22 14:48:32',NULL),(2,102,'批量导入成员','company_export_member','/permission/api/v1/company/member/export',1,1,'2023-05-22 10:33:42','2023-05-24 14:17:06',NULL),(3,103,'编辑成员','company_update_member','/permission/api/v1/company/member/update',1,1,'2023-05-22 10:33:42','2023-05-22 14:48:35',NULL),(4,104,'删除成员','company_remove_member','/permission/api/v1/company/member/remove',1,1,'2023-05-22 10:33:42','2023-05-22 14:48:37',NULL),(5,105,'更改企业角色','company_set_role_member','/permission/api/v1/role/company/set',1,1,'2023-05-22 10:33:42','2023-05-22 16:39:34',NULL),(6,201,'新建团队','team_save','/permission/api/v1/team/save',1,2,'2023-05-22 10:35:38','2023-05-22 14:48:40',NULL),(7,202,'编辑团队','team_update','/permission/api/v1/team/update',1,2,'2023-05-22 10:35:38','2023-05-22 14:48:41',NULL),(8,203,'添加团队成员','team_save_member','/permission/api/v1/team/member/save',1,2,'2023-05-22 10:35:38','2023-05-22 14:48:42',NULL),(9,204,'移除团队成员','team_remove_member','/permission/api/v1/team/member/remove',1,2,'2023-05-22 10:35:38','2023-05-22 14:48:44',NULL),(10,205,'更改团队角色','team_set_role_member','/permission/api/v1/role/team/set',1,2,'2023-05-22 10:35:38','2023-05-29 14:42:11',NULL),(11,206,'解散团队','team_disband','/permission/api/v1/team/disband',1,2,'2023-05-22 10:35:38','2023-05-22 14:48:46',NULL),(12,301,'新建角色','role_save','/permission/api/v1/role/save',1,3,'2023-05-22 10:36:40','2023-05-22 14:48:47',NULL),(13,302,'设置角色权限','role_set','/permission/api/v1/permission/role/set',1,3,'2023-05-22 10:36:40','2023-05-22 14:48:51',NULL),(14,303,'删除角色','role_remove','/permission/api/v1/role/remove',1,3,'2023-05-22 10:36:40','2023-05-22 14:48:54',NULL),(15,401,'新建第三方集成','notice_save','/permission/api/v1/notice/save',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL),(16,402,'修改第三方集成','notice_update','/permission/api/v1/notice/update',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL),(17,403,'禁用|启用第三方集成','notice_set_status','/permission/api/v1/notice/set_status',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL),(18,404,'删除第三方集成','notice_remove','/permission/api/v1/notice/remove',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL),(19,405,'新建通知组','notice_group_save','/permission/api/v1/notice/group/save',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL),(20,406,'修改通知组','notice_group_update','/permission/api/v1/notice/group/update',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL),(21,407,'删除通知组','notice_group_remove','/permission/api/v1/notice/group/remove',1,4,'2023-07-12 16:56:47','2023-07-12 16:56:47',NULL);
/*!40000 ALTER TABLE `permission` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `preinstall_conf`
--

DROP TABLE IF EXISTS `preinstall_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `preinstall_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `conf_name` varchar(100) NOT NULL COMMENT '配置名称',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '用户ID',
  `user_name` varchar(64) NOT NULL COMMENT '用户名称',
  `task_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '任务类型',
  `task_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '压测模式',
  `control_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '控制模式：0-集中模式，1-单独模式',
  `debug_mode` varchar(100) NOT NULL DEFAULT 'stop' COMMENT 'debug模式：stop-关闭，all-开启全部日志，only_success-开启仅成功日志，only_error-开启仅错误日志',
  `mode_conf` text NOT NULL COMMENT '压测配置详情',
  `timed_task_conf` text NOT NULL COMMENT '定时任务相关配置',
  `is_open_distributed` tinyint(2) NOT NULL DEFAULT '0' COMMENT '是否开启分布式调度：0-关闭，1-开启',
  `machine_dispatch_mode_conf` text NOT NULL COMMENT '分布式压力机配置',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` timestamp NULL DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预设配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `preinstall_conf`
--

LOCK TABLES `preinstall_conf` WRITE;
/*!40000 ALTER TABLE `preinstall_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `preinstall_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `public_function`
--

DROP TABLE IF EXISTS `public_function`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `public_function` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `function` varchar(255) NOT NULL COMMENT '函数',
  `function_name` varchar(255) NOT NULL COMMENT '函数名称',
  `remark` text NOT NULL COMMENT '备注',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='公共函数表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `public_function`
--

LOCK TABLES `public_function` WRITE;
/*!40000 ALTER TABLE `public_function` DISABLE KEYS */;
/*!40000 ALTER TABLE `public_function` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `report_machine`
--

DROP TABLE IF EXISTS `report_machine`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `report_machine` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `report_id` varchar(100) NOT NULL COMMENT '报告id',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `ip` varchar(15) NOT NULL COMMENT '机器ip',
  `concurrency` bigint(20) NOT NULL DEFAULT '0' COMMENT '并发数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_report_id` (`report_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `report_machine`
--

LOCK TABLES `report_machine` WRITE;
/*!40000 ALTER TABLE `report_machine` DISABLE KEYS */;
/*!40000 ALTER TABLE `report_machine` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `role`
--

DROP TABLE IF EXISTS `role`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `role` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `role_id` varchar(100) NOT NULL COMMENT '角色id',
  `role_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '角色分类（1：企业  2：团队）',
  `name` varchar(100) NOT NULL DEFAULT '' COMMENT '角色名称',
  `company_id` varchar(100) NOT NULL DEFAULT '' COMMENT '企业id',
  `level` tinyint(2) NOT NULL DEFAULT '0' COMMENT '角色层级（1:超管/团队管理员 2:管理员/团队成员 3:普通成员/只读成员/自定义角色） ',
  `is_default` tinyint(2) NOT NULL DEFAULT '2' COMMENT '是否是默认角色  1：是   2：自定义角色',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_role_id` (`role_id`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COMMENT='角色表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `role`
--

LOCK TABLES `role` WRITE;
/*!40000 ALTER TABLE `role` DISABLE KEYS */;
INSERT INTO `role` VALUES (1,'85967f8f-5bca-4eaf-8766-19f15b6c0536',1,'超管','12e1c2a9-5637-48af-ad20-00e286fa241f',1,1,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(2,'f290837a-05fa-4a5c-be3a-8aab708378b6',1,'管理员','12e1c2a9-5637-48af-ad20-00e286fa241f',2,1,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(3,'a8e2ea45-fbf6-424b-bdef-575014221ce5',1,'普通成员','12e1c2a9-5637-48af-ad20-00e286fa241f',3,1,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(4,'89c0470e-e10a-4e97-8d3f-ff182562b8ad',2,'团队管理员','12e1c2a9-5637-48af-ad20-00e286fa241f',1,1,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(5,'e371adac-988b-4065-a8ad-76a348fc0f86',2,'团队成员','12e1c2a9-5637-48af-ad20-00e286fa241f',2,1,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL);
/*!40000 ALTER TABLE `role` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `role_permission`
--

DROP TABLE IF EXISTS `role_permission`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `role_permission` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `role_id` varchar(100) NOT NULL COMMENT '角色id',
  `permission_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '权限id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_role_id` (`role_id`)
) ENGINE=InnoDB AUTO_INCREMENT=49 DEFAULT CHARSET=utf8mb4 COMMENT='角色权限表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `role_permission`
--

LOCK TABLES `role_permission` WRITE;
/*!40000 ALTER TABLE `role_permission` DISABLE KEYS */;
INSERT INTO `role_permission` VALUES (1,'85967f8f-5bca-4eaf-8766-19f15b6c0536',101,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(2,'85967f8f-5bca-4eaf-8766-19f15b6c0536',102,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(3,'85967f8f-5bca-4eaf-8766-19f15b6c0536',103,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(4,'85967f8f-5bca-4eaf-8766-19f15b6c0536',104,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(5,'85967f8f-5bca-4eaf-8766-19f15b6c0536',105,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(6,'85967f8f-5bca-4eaf-8766-19f15b6c0536',201,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(7,'85967f8f-5bca-4eaf-8766-19f15b6c0536',202,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(8,'85967f8f-5bca-4eaf-8766-19f15b6c0536',203,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(9,'85967f8f-5bca-4eaf-8766-19f15b6c0536',204,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(10,'85967f8f-5bca-4eaf-8766-19f15b6c0536',205,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(11,'85967f8f-5bca-4eaf-8766-19f15b6c0536',206,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(12,'85967f8f-5bca-4eaf-8766-19f15b6c0536',301,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(13,'85967f8f-5bca-4eaf-8766-19f15b6c0536',302,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(14,'85967f8f-5bca-4eaf-8766-19f15b6c0536',303,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(15,'85967f8f-5bca-4eaf-8766-19f15b6c0536',401,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(16,'85967f8f-5bca-4eaf-8766-19f15b6c0536',402,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(17,'85967f8f-5bca-4eaf-8766-19f15b6c0536',403,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(18,'85967f8f-5bca-4eaf-8766-19f15b6c0536',404,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(19,'85967f8f-5bca-4eaf-8766-19f15b6c0536',405,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(20,'85967f8f-5bca-4eaf-8766-19f15b6c0536',406,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(21,'85967f8f-5bca-4eaf-8766-19f15b6c0536',407,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(22,'f290837a-05fa-4a5c-be3a-8aab708378b6',101,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(23,'f290837a-05fa-4a5c-be3a-8aab708378b6',102,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(24,'f290837a-05fa-4a5c-be3a-8aab708378b6',103,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(25,'f290837a-05fa-4a5c-be3a-8aab708378b6',104,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(26,'f290837a-05fa-4a5c-be3a-8aab708378b6',105,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(27,'f290837a-05fa-4a5c-be3a-8aab708378b6',201,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(28,'f290837a-05fa-4a5c-be3a-8aab708378b6',202,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(29,'f290837a-05fa-4a5c-be3a-8aab708378b6',203,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(30,'f290837a-05fa-4a5c-be3a-8aab708378b6',204,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(31,'f290837a-05fa-4a5c-be3a-8aab708378b6',205,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(32,'f290837a-05fa-4a5c-be3a-8aab708378b6',206,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(33,'f290837a-05fa-4a5c-be3a-8aab708378b6',301,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(34,'f290837a-05fa-4a5c-be3a-8aab708378b6',302,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(35,'f290837a-05fa-4a5c-be3a-8aab708378b6',303,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(36,'f290837a-05fa-4a5c-be3a-8aab708378b6',401,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(37,'f290837a-05fa-4a5c-be3a-8aab708378b6',402,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(38,'f290837a-05fa-4a5c-be3a-8aab708378b6',403,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(39,'f290837a-05fa-4a5c-be3a-8aab708378b6',404,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(40,'f290837a-05fa-4a5c-be3a-8aab708378b6',405,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(41,'f290837a-05fa-4a5c-be3a-8aab708378b6',406,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(42,'f290837a-05fa-4a5c-be3a-8aab708378b6',407,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(43,'89c0470e-e10a-4e97-8d3f-ff182562b8ad',202,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(44,'89c0470e-e10a-4e97-8d3f-ff182562b8ad',203,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(45,'89c0470e-e10a-4e97-8d3f-ff182562b8ad',204,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(46,'89c0470e-e10a-4e97-8d3f-ff182562b8ad',205,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(47,'89c0470e-e10a-4e97-8d3f-ff182562b8ad',206,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(48,'e371adac-988b-4065-a8ad-76a348fc0f86',203,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL);
/*!40000 ALTER TABLE `role_permission` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `role_type_permission`
--

DROP TABLE IF EXISTS `role_type_permission`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `role_type_permission` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `role_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '角色分类（1：企业  2：团队）',
  `permission_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '权限id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=27 DEFAULT CHARSET=utf8mb4 COMMENT='角色分类可拥有的权限';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `role_type_permission`
--

LOCK TABLES `role_type_permission` WRITE;
/*!40000 ALTER TABLE `role_type_permission` DISABLE KEYS */;
INSERT INTO `role_type_permission` VALUES (1,1,101,'2023-05-22 17:13:31','2023-05-22 17:14:05',NULL),(2,1,102,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(3,1,103,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(4,1,104,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(5,1,105,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(6,1,201,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(7,1,202,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(8,1,203,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(9,1,204,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(10,1,205,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(11,1,206,'2023-05-25 18:57:51','2023-05-25 18:57:51',NULL),(12,1,301,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(13,1,302,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(14,1,303,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(15,2,202,'2023-05-22 17:16:00','2023-05-22 17:16:00',NULL),(16,2,203,'2023-05-22 17:16:01','2023-05-22 17:16:01',NULL),(17,2,204,'2023-05-24 15:22:43','2023-05-24 15:22:43',NULL),(18,2,205,'2023-05-22 17:16:01','2023-05-22 17:16:01',NULL),(19,2,206,'2023-05-22 17:16:01','2023-05-22 17:16:01',NULL),(20,1,401,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL),(21,1,402,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL),(22,1,403,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL),(23,1,404,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL),(24,1,405,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL),(25,1,406,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL),(26,1,407,'2023-07-12 17:37:50','2023-07-12 17:37:50',NULL);
/*!40000 ALTER TABLE `role_type_permission` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `scene_variable`
--

DROP TABLE IF EXISTS `scene_variable`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `scene_variable` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `type` tinyint(4) NOT NULL DEFAULT '0' COMMENT '使用范围：1-全局变量，2-场景变量',
  `var` varchar(255) NOT NULL COMMENT '变量名',
  `val` text NOT NULL COMMENT '变量值',
  `description` text NOT NULL COMMENT '描述',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '开关状态：1-开启，2-关闭',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`),
  KEY `idx_scene_id` (`scene_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设置变量表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `scene_variable`
--

LOCK TABLES `scene_variable` WRITE;
/*!40000 ALTER TABLE `scene_variable` DISABLE KEYS */;
/*!40000 ALTER TABLE `scene_variable` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `setting`
--

DROP TABLE IF EXISTS `setting`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `setting` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `user_id` varchar(100) NOT NULL COMMENT '用户id',
  `team_id` varchar(100) NOT NULL COMMENT '当前团队id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='设置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `setting`
--

LOCK TABLES `setting` WRITE;
/*!40000 ALTER TABLE `setting` DISABLE KEYS */;
INSERT INTO `setting` VALUES (1,'d0291907-03f6-4be2-b536-bfbe10a4cfa9','e64b9392-7ab9-4d94-a6d7-00acd96540af','2026-08-29 08:53:54','2026-08-29 09:49:23',NULL);
/*!40000 ALTER TABLE `setting` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `sms_log`
--

DROP TABLE IF EXISTS `sms_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `sms_log` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `type` tinyint(2) NOT NULL COMMENT '短信类型: 1-注册，2-登录，3-找回密码',
  `mobile` char(11) NOT NULL DEFAULT '' COMMENT '手机号',
  `content` varchar(200) NOT NULL COMMENT '短信内容',
  `verify_code` varchar(20) NOT NULL COMMENT '验证码',
  `verify_code_expiration_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '验证码有效时间',
  `client_ip` varchar(100) NOT NULL DEFAULT '' COMMENT '客户端IP',
  `send_status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '发送状态：1-成功 2-失败',
  `verify_status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '校验状态：1-未校验 2-已校验',
  `send_response` text NOT NULL COMMENT '短信服务响应',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_type_mobile_verify_code` (`type`,`mobile`,`verify_code`,`verify_code_expiration_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='短信发送记录表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `sms_log`
--

LOCK TABLES `sms_log` WRITE;
/*!40000 ALTER TABLE `sms_log` DISABLE KEYS */;
/*!40000 ALTER TABLE `sms_log` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `stress_plan`
--

DROP TABLE IF EXISTS `stress_plan`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `stress_plan` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `rank_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '序号ID',
  `plan_name` varchar(255) NOT NULL COMMENT '计划名称',
  `task_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '计划类型：1-普通任务，2-定时任务',
  `task_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '压测类型: 1-并发模式，2-阶梯模式，3-错误率模式，4-响应时间模式，5-每秒请求数模式，6-每秒事务数模式',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '计划状态1:未开始,2:进行中',
  `create_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '创建人id',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人id',
  `remark` text NOT NULL COMMENT '备注',
  `run_count` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '运行次数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='性能计划表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `stress_plan`
--

LOCK TABLES `stress_plan` WRITE;
/*!40000 ALTER TABLE `stress_plan` DISABLE KEYS */;
/*!40000 ALTER TABLE `stress_plan` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `stress_plan_email`
--

DROP TABLE IF EXISTS `stress_plan_email`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `stress_plan_email` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键',
  `plan_id` varchar(100) NOT NULL COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `email` varchar(255) DEFAULT NULL COMMENT '邮箱',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='性能计划收件人';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `stress_plan_email`
--

LOCK TABLES `stress_plan_email` WRITE;
/*!40000 ALTER TABLE `stress_plan_email` DISABLE KEYS */;
/*!40000 ALTER TABLE `stress_plan_email` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `stress_plan_report`
--

DROP TABLE IF EXISTS `stress_plan_report`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `stress_plan_report` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `report_id` varchar(100) NOT NULL COMMENT '报告ID',
  `report_name` varchar(125) NOT NULL COMMENT '报告名称',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `plan_id` varchar(100) NOT NULL COMMENT '计划ID',
  `rank_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '序号ID',
  `plan_name` varchar(255) NOT NULL COMMENT '计划名称',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `scene_name` varchar(255) NOT NULL COMMENT '场景名称',
  `task_type` int(11) NOT NULL COMMENT '任务类型',
  `task_mode` int(11) NOT NULL COMMENT '压测模式',
  `control_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '控制模式：0-集中模式，1-单独模式',
  `debug_mode` varchar(100) NOT NULL DEFAULT 'stop' COMMENT 'debug模式：stop-关闭，all-开启全部日志，only_success-开启仅成功日志，only_error-开启仅错误日志',
  `run_duration_time` bigint(20) NOT NULL DEFAULT '0' COMMENT '任务运行持续时长',
  `status` tinyint(4) NOT NULL COMMENT '报告状态1:进行中，2:已完成',
  `remark` text NOT NULL COMMENT '备注',
  `run_user_id` varchar(100) NOT NULL COMMENT '启动人id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间（执行时间）',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_report_id` (`report_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='性能测试报告表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `stress_plan_report`
--

LOCK TABLES `stress_plan_report` WRITE;
/*!40000 ALTER TABLE `stress_plan_report` DISABLE KEYS */;
/*!40000 ALTER TABLE `stress_plan_report` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `stress_plan_task_conf`
--

DROP TABLE IF EXISTS `stress_plan_task_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `stress_plan_task_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '配置ID',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `task_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '任务类型：1-普通模式，2-定时任务',
  `task_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '压测模式：1-并发模式，2-阶梯模式，3-错误率模式，4-响应时间模式，5-每秒请求数模式，6-每秒事务数模式',
  `control_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '控制模式：0-集中模式，1-单独模式',
  `debug_mode` varchar(100) NOT NULL DEFAULT 'stop' COMMENT 'debug模式：stop-关闭，all-开启全部日志，only_success-开启仅成功日志，only_error-开启仅错误日志',
  `mode_conf` text NOT NULL COMMENT '压测模式配置详情',
  `is_open_distributed` tinyint(2) NOT NULL DEFAULT '0' COMMENT '是否开启分布式调度：0-关闭，1-开启',
  `machine_dispatch_mode_conf` text NOT NULL COMMENT '分布式压力机配置',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人用户ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_scene_id` (`scene_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='性能计划—普通任务配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `stress_plan_task_conf`
--

LOCK TABLES `stress_plan_task_conf` WRITE;
/*!40000 ALTER TABLE `stress_plan_task_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `stress_plan_task_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `stress_plan_timed_task_conf`
--

DROP TABLE IF EXISTS `stress_plan_timed_task_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `stress_plan_timed_task_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '表id',
  `plan_id` varchar(100) NOT NULL COMMENT '计划id',
  `scene_id` varchar(100) NOT NULL COMMENT '场景id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `user_id` varchar(100) NOT NULL COMMENT '用户ID',
  `frequency` int(10) unsigned NOT NULL DEFAULT '0' COMMENT '任务执行频次: 0-一次，1-每天，2-每周，3-每月',
  `task_exec_time` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '任务执行时间',
  `task_close_time` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '任务结束时间',
  `task_type` tinyint(2) NOT NULL DEFAULT '2' COMMENT '任务类型：1-普通任务，2-定时任务',
  `task_mode` tinyint(2) NOT NULL DEFAULT '1' COMMENT '压测模式：1-并发模式，2-阶梯模式，3-错误率模式，4-响应时间模式，5-每秒请求数模式，6 -每秒事务数模式',
  `control_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '控制模式：0-集中模式，1-单独模式',
  `debug_mode` varchar(100) NOT NULL DEFAULT 'stop' COMMENT 'debug模式：stop-关闭，all-开启全部日志，only_success-开启仅成功日志，only_error-开启仅错误日志',
  `mode_conf` text NOT NULL COMMENT '压测详细配置',
  `is_open_distributed` tinyint(2) NOT NULL DEFAULT '0' COMMENT '是否开启分布式调度：0-关闭，1-开启',
  `machine_dispatch_mode_conf` text NOT NULL COMMENT '分布式压力机配置',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人ID',
  `status` tinyint(11) NOT NULL DEFAULT '0' COMMENT '任务状态：0-未启用，1-运行中，2-已过期',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_scene_id` (`scene_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='性能计划-定时任务配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `stress_plan_timed_task_conf`
--

LOCK TABLES `stress_plan_timed_task_conf` WRITE;
/*!40000 ALTER TABLE `stress_plan_timed_task_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `stress_plan_timed_task_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `target`
--

DROP TABLE IF EXISTS `target`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `target` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `target_id` varchar(100) NOT NULL COMMENT '全局唯一ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `target_type` varchar(10) NOT NULL COMMENT '类型：文件夹，接口，分组，场景,测试用例',
  `name` varchar(255) NOT NULL COMMENT '名称',
  `parent_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '父级ID',
  `method` varchar(16) NOT NULL COMMENT '方法',
  `sort` int(11) NOT NULL DEFAULT '0' COMMENT '排序',
  `type_sort` int(11) NOT NULL DEFAULT '0' COMMENT '类型排序',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '回收站状态：1-正常，2-回收站',
  `version` int(11) NOT NULL DEFAULT '0' COMMENT '产品版本号',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人ID',
  `recent_user_id` varchar(100) NOT NULL COMMENT '最近修改人ID',
  `description` text NOT NULL COMMENT '备注',
  `source` tinyint(4) NOT NULL DEFAULT '0' COMMENT '数据来源：0-测试对象，1-场景管理，2-性能，3-自动化测试， 4-mock',
  `plan_id` varchar(100) NOT NULL COMMENT '计划id',
  `source_id` varchar(100) NOT NULL COMMENT '引用来源ID',
  `is_checked` tinyint(2) NOT NULL DEFAULT '1' COMMENT '是否开启：1-开启，2-关闭',
  `is_disabled` tinyint(2) NOT NULL DEFAULT '0' COMMENT '运行计划时是否禁用：0-不禁用，1-禁用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_target_id` (`target_id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='创建目标';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `target`
--

LOCK TABLES `target` WRITE;
/*!40000 ALTER TABLE `target` DISABLE KEYS */;
/*!40000 ALTER TABLE `target` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `team`
--

DROP TABLE IF EXISTS `team`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `team` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `name` varchar(64) NOT NULL COMMENT '团队名称',
  `description` text COMMENT '团队描述',
  `company_id` varchar(100) NOT NULL DEFAULT '' COMMENT '所属企业id',
  `type` tinyint(4) NOT NULL COMMENT '团队类型 1: 私有团队；2: 普通团队',
  `trial_expiration_date` datetime NOT NULL COMMENT '试用有效期',
  `is_vip` tinyint(2) NOT NULL DEFAULT '1' COMMENT '是否为付费团队 1-否 2-是',
  `vip_expiration_date` datetime NOT NULL COMMENT '付费有效期',
  `vum_num` bigint(20) NOT NULL DEFAULT '0' COMMENT '当前可用VUM总数',
  `max_user_num` bigint(20) NOT NULL DEFAULT '0' COMMENT '当前团队最大成员数量',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建者id',
  `team_buy_version_type` int(10) NOT NULL DEFAULT '1' COMMENT '团队套餐类型：1-个人版，2-团队版，3-企业版，4-私有化部署',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='团队表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `team`
--

LOCK TABLES `team` WRITE;
/*!40000 ALTER TABLE `team` DISABLE KEYS */;
INSERT INTO `team` VALUES (1,'e64b9392-7ab9-4d94-a6d7-00acd96540af','车抵贷项目','','12e1c2a9-5637-48af-ad20-00e286fa241f',2,'0000-00-00 00:00:00',1,'0000-00-00 00:00:00',0,0,'d0291907-03f6-4be2-b536-bfbe10a4cfa9',1,'2026-08-29 08:56:13','2026-08-29 08:56:13',NULL);
/*!40000 ALTER TABLE `team` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `team_env`
--

DROP TABLE IF EXISTS `team_env`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `team_env` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `name` varchar(100) NOT NULL COMMENT '环境名称',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='环境管理表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `team_env`
--

LOCK TABLES `team_env` WRITE;
/*!40000 ALTER TABLE `team_env` DISABLE KEYS */;
/*!40000 ALTER TABLE `team_env` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `team_env_database`
--

DROP TABLE IF EXISTS `team_env_database`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `team_env_database` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `team_env_id` bigint(20) NOT NULL COMMENT '环境变量id',
  `type` varchar(100) NOT NULL COMMENT '数据库类型',
  `server_name` varchar(100) NOT NULL COMMENT 'mysql服务名称',
  `host` varchar(200) NOT NULL COMMENT '服务地址',
  `port` int(11) NOT NULL COMMENT '端口号',
  `user` varchar(100) NOT NULL COMMENT '账号',
  `password` varchar(200) NOT NULL COMMENT '密码',
  `db_name` varchar(100) NOT NULL COMMENT '数据库名称',
  `charset` varchar(100) NOT NULL DEFAULT 'utf8mb4' COMMENT '字符编码集',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`),
  KEY `idx_team_env_id` (`team_env_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Sql数据库服务基础信息表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `team_env_database`
--

LOCK TABLES `team_env_database` WRITE;
/*!40000 ALTER TABLE `team_env_database` DISABLE KEYS */;
/*!40000 ALTER TABLE `team_env_database` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `team_env_service`
--

DROP TABLE IF EXISTS `team_env_service`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `team_env_service` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `team_env_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '环境id',
  `name` varchar(100) NOT NULL COMMENT '服务名称',
  `content` varchar(200) NOT NULL COMMENT '服务URL',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idxx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='团队环境服务管理';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `team_env_service`
--

LOCK TABLES `team_env_service` WRITE;
/*!40000 ALTER TABLE `team_env_service` DISABLE KEYS */;
/*!40000 ALTER TABLE `team_env_service` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `team_user_queue`
--

DROP TABLE IF EXISTS `team_user_queue`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `team_user_queue` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `email` varchar(255) NOT NULL COMMENT '邮箱',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='邀请待注册队列';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `team_user_queue`
--

LOCK TABLES `team_user_queue` WRITE;
/*!40000 ALTER TABLE `team_user_queue` DISABLE KEYS */;
/*!40000 ALTER TABLE `team_user_queue` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `third_notice`
--

DROP TABLE IF EXISTS `third_notice`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `third_notice` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `notice_id` varchar(100) NOT NULL COMMENT '通知id',
  `name` varchar(100) NOT NULL DEFAULT '' COMMENT '通知名称',
  `channel_id` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '三方通知渠道id',
  `params` json DEFAULT NULL COMMENT '通知参数',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '1:启用 2:禁用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_notice_id` (`notice_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='三方通知设置';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `third_notice`
--

LOCK TABLES `third_notice` WRITE;
/*!40000 ALTER TABLE `third_notice` DISABLE KEYS */;
/*!40000 ALTER TABLE `third_notice` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `third_notice_channel`
--

DROP TABLE IF EXISTS `third_notice_channel`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `third_notice_channel` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `name` varchar(100) NOT NULL DEFAULT '' COMMENT '名称',
  `type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '类型 1:飞书  2:企业微信  3:邮箱  4:钉钉',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COMMENT='三方通知渠道';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `third_notice_channel`
--

LOCK TABLES `third_notice_channel` WRITE;
/*!40000 ALTER TABLE `third_notice_channel` DISABLE KEYS */;
INSERT INTO `third_notice_channel` VALUES (1,'飞书群机器人',1,'2023-06-21 10:46:03','2023-06-21 10:46:03',NULL),(2,'飞书企业应用',1,'2023-06-21 10:46:25','2023-06-21 10:46:25',NULL),(3,'企业微信应用',2,'2023-06-21 10:46:39','2023-06-21 10:46:53',NULL),(4,'企业微信机器人',2,'2023-06-21 10:47:08','2023-06-21 10:47:08',NULL),(5,'邮箱',3,'2023-06-29 11:03:45','2023-06-29 11:03:45',NULL),(6,'钉钉群机器人',4,'2023-06-29 11:03:55','2023-06-29 11:04:00',NULL),(7,'钉钉企业应用',4,'2023-06-29 11:04:13','2023-06-29 11:04:13',NULL);
/*!40000 ALTER TABLE `third_notice_channel` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `third_notice_group`
--

DROP TABLE IF EXISTS `third_notice_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `third_notice_group` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `group_id` varchar(100) NOT NULL COMMENT '通知组id',
  `name` varchar(100) NOT NULL DEFAULT '' COMMENT '通知组名称',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='三方通知组表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `third_notice_group`
--

LOCK TABLES `third_notice_group` WRITE;
/*!40000 ALTER TABLE `third_notice_group` DISABLE KEYS */;
/*!40000 ALTER TABLE `third_notice_group` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `third_notice_group_event`
--

DROP TABLE IF EXISTS `third_notice_group_event`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `third_notice_group_event` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `group_id` varchar(100) NOT NULL DEFAULT '' COMMENT '通知组id',
  `event_id` int(11) NOT NULL DEFAULT '0' COMMENT '事件id',
  `plan_id` varchar(100) NOT NULL DEFAULT '' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL DEFAULT '' COMMENT '团队ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='三方通知组触发事件表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `third_notice_group_event`
--

LOCK TABLES `third_notice_group_event` WRITE;
/*!40000 ALTER TABLE `third_notice_group_event` DISABLE KEYS */;
/*!40000 ALTER TABLE `third_notice_group_event` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `third_notice_group_relate`
--

DROP TABLE IF EXISTS `third_notice_group_relate`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `third_notice_group_relate` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `group_id` varchar(100) NOT NULL COMMENT '通知组id',
  `notice_id` varchar(100) NOT NULL COMMENT '通知id',
  `params` json DEFAULT NULL COMMENT '通知目标参数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_notice_id` (`notice_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='三方通知组通知关联表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `third_notice_group_relate`
--

LOCK TABLES `third_notice_group_relate` WRITE;
/*!40000 ALTER TABLE `third_notice_group_relate` DISABLE KEYS */;
/*!40000 ALTER TABLE `third_notice_group_relate` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_plan`
--

DROP TABLE IF EXISTS `ui_plan`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_plan` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `rank_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '序号ID',
  `name` varchar(255) NOT NULL COMMENT '计划名称',
  `task_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '计划类型：1-普通任务，2-定时任务',
  `create_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '创建人id',
  `head_user_id` varchar(1000) NOT NULL DEFAULT '0' COMMENT '负责人id ,用分割',
  `run_count` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '运行次数',
  `init_strategy` tinyint(2) NOT NULL DEFAULT '1' COMMENT '初始化策略：1-计划执行前重启浏览器，2-场景执行前重启浏览器，3-无初始化',
  `description` text NOT NULL COMMENT '备注',
  `browsers` json DEFAULT NULL COMMENT '浏览器信息',
  `ui_machine_key` varchar(255) NOT NULL DEFAULT '' COMMENT '指定机器key',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI计划表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_plan`
--

LOCK TABLES `ui_plan` WRITE;
/*!40000 ALTER TABLE `ui_plan` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_plan` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_plan_report`
--

DROP TABLE IF EXISTS `ui_plan_report`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_plan_report` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `report_id` varchar(100) NOT NULL COMMENT '报告ID',
  `report_name` varchar(125) NOT NULL COMMENT '报告名称',
  `plan_id` varchar(100) NOT NULL COMMENT '计划ID',
  `plan_name` varchar(255) NOT NULL COMMENT '计划名称',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `rank_id` bigint(20) NOT NULL DEFAULT '0' COMMENT '序号ID',
  `task_type` int(11) NOT NULL DEFAULT '0' COMMENT '任务类型',
  `scene_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '场景运行次序：1-顺序执行，2-同时执行',
  `run_duration_time` bigint(20) NOT NULL DEFAULT '0' COMMENT '任务运行持续时长',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '报告状态1:进行中，2:已完成',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '启动人id',
  `remark` text NOT NULL COMMENT '备注',
  `browsers` json DEFAULT NULL COMMENT '浏览器信息',
  `ui_machine_key` varchar(255) NOT NULL DEFAULT '' COMMENT '指定机器key',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间（执行时间）',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_report_id` (`report_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化测试计划-报告表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_plan_report`
--

LOCK TABLES `ui_plan_report` WRITE;
/*!40000 ALTER TABLE `ui_plan_report` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_plan_report` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_plan_task_conf`
--

DROP TABLE IF EXISTS `ui_plan_task_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_plan_task_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '配置ID',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队ID',
  `task_type` tinyint(2) NOT NULL DEFAULT '0' COMMENT '任务类型：1-普通模式，2-定时任务',
  `scene_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '场景运行次序：1-顺序执行，2-同时执行',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人用户ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化测试—普通任务配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_plan_task_conf`
--

LOCK TABLES `ui_plan_task_conf` WRITE;
/*!40000 ALTER TABLE `ui_plan_task_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_plan_task_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_plan_timed_task_conf`
--

DROP TABLE IF EXISTS `ui_plan_timed_task_conf`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_plan_timed_task_conf` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT COMMENT '表id',
  `plan_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '计划id',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `frequency` int(10) unsigned NOT NULL DEFAULT '0' COMMENT '任务执行频次: 0-一次，1-每天，2-每周，3-每月，4-固定时间间隔',
  `task_exec_time` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '任务执行时间',
  `task_close_time` bigint(20) unsigned NOT NULL DEFAULT '0' COMMENT '任务结束时间',
  `fixed_interval_start_time` bigint(20) NOT NULL DEFAULT '0' COMMENT '固定时间间隔开始时间',
  `fixed_interval_time` int(10) NOT NULL DEFAULT '0' COMMENT '固定间隔时间',
  `fixed_run_num` int(10) NOT NULL DEFAULT '0' COMMENT '固定执行次数',
  `fixed_interval_time_type` int(10) NOT NULL DEFAULT '0' COMMENT '固定间隔时间类型：0-分钟，1-小时',
  `task_type` tinyint(2) NOT NULL DEFAULT '2' COMMENT '任务类型：1-普通任务，2-定时任务',
  `scene_run_order` tinyint(2) NOT NULL DEFAULT '1' COMMENT '场景运行次序：1-顺序执行，2-同时执行',
  `status` tinyint(2) NOT NULL DEFAULT '0' COMMENT '任务状态：0-未启用，1-运行中，2-已过期',
  `run_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '运行人用户ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_plan_id` (`plan_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化测试-定时任务配置表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_plan_timed_task_conf`
--

LOCK TABLES `ui_plan_timed_task_conf` WRITE;
/*!40000 ALTER TABLE `ui_plan_timed_task_conf` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_plan_timed_task_conf` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_scene`
--

DROP TABLE IF EXISTS `ui_scene`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_scene` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `scene_id` varchar(100) NOT NULL COMMENT '全局唯一ID',
  `scene_type` varchar(10) NOT NULL COMMENT '类型：文件夹，场景',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `name` varchar(255) NOT NULL COMMENT '名称',
  `parent_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '父级ID',
  `sort` int(11) NOT NULL DEFAULT '0' COMMENT '排序',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '回收站状态：1-正常，2-回收站',
  `version` int(11) NOT NULL DEFAULT '0' COMMENT '产品版本号',
  `source` tinyint(2) NOT NULL DEFAULT '1' COMMENT '数据来源：1-场景管理，2-计划',
  `plan_id` varchar(255) NOT NULL DEFAULT '' COMMENT '计划ID',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人ID',
  `recent_user_id` varchar(100) NOT NULL COMMENT '最近修改人ID',
  `description` text NOT NULL COMMENT '备注',
  `ui_machine_key` varchar(255) NOT NULL DEFAULT '' COMMENT '指定执行的UI自动化机器key',
  `source_id` varchar(100) NOT NULL COMMENT '引用来源ID',
  `browsers` json DEFAULT NULL COMMENT '浏览器信息',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_scene_id` (`scene_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化场景';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_scene`
--

LOCK TABLES `ui_scene` WRITE;
/*!40000 ALTER TABLE `ui_scene` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_scene` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_scene_element`
--

DROP TABLE IF EXISTS `ui_scene_element`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_scene_element` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `operator_id` varchar(100) NOT NULL COMMENT '操作ID',
  `element_id` varchar(100) NOT NULL COMMENT '元素ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '状态 1：正常  2：回收站',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_scene_id` (`scene_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化场景元素关联表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_scene_element`
--

LOCK TABLES `ui_scene_element` WRITE;
/*!40000 ALTER TABLE `ui_scene_element` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_scene_element` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_scene_operator`
--

DROP TABLE IF EXISTS `ui_scene_operator`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_scene_operator` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `operator_id` varchar(100) NOT NULL COMMENT '全局唯一ID',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `name` varchar(255) NOT NULL COMMENT '名称',
  `parent_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '父级ID',
  `sort` int(11) NOT NULL DEFAULT '0' COMMENT '排序',
  `status` tinyint(4) NOT NULL DEFAULT '1' COMMENT '状态：1-正常，2-禁用',
  `type` varchar(100) NOT NULL DEFAULT '' COMMENT '步骤类型',
  `action` varchar(100) NOT NULL DEFAULT '' COMMENT '步骤方法',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_scene_id` (`scene_id`),
  KEY `idx_operator_id` (`operator_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化场景步骤';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_scene_operator`
--

LOCK TABLES `ui_scene_operator` WRITE;
/*!40000 ALTER TABLE `ui_scene_operator` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_scene_operator` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_scene_sync`
--

DROP TABLE IF EXISTS `ui_scene_sync`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_scene_sync` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `source_scene_id` varchar(100) NOT NULL COMMENT '引用场景ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `sync_mode` tinyint(2) NOT NULL DEFAULT '0' COMMENT '状态：1-实时，2-手动,已场景为准   3-手动,已计划为准',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_scene_id` (`scene_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI场景同步关系表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_scene_sync`
--

LOCK TABLES `ui_scene_sync` WRITE;
/*!40000 ALTER TABLE `ui_scene_sync` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_scene_sync` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ui_scene_trash`
--

DROP TABLE IF EXISTS `ui_scene_trash`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `ui_scene_trash` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT 'id',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `created_user_id` varchar(100) NOT NULL COMMENT '创建人ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_scene_id` (`scene_id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UI自动化场景回收站';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ui_scene_trash`
--

LOCK TABLES `ui_scene_trash` WRITE;
/*!40000 ALTER TABLE `ui_scene_trash` DISABLE KEYS */;
/*!40000 ALTER TABLE `ui_scene_trash` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `user`
--

DROP TABLE IF EXISTS `user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `user` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `user_id` varchar(100) NOT NULL COMMENT '用户id',
  `account` varchar(100) NOT NULL DEFAULT '' COMMENT '账号',
  `email` varchar(100) NOT NULL COMMENT '邮箱',
  `mobile` char(11) NOT NULL COMMENT '手机号',
  `password` varchar(255) NOT NULL COMMENT '密码',
  `nickname` varchar(64) NOT NULL COMMENT '昵称',
  `avatar` varchar(255) DEFAULT NULL COMMENT '头像',
  `wechat_open_id` varchar(100) NOT NULL COMMENT '微信开放的唯一id',
  `utm_source` varchar(50) NOT NULL COMMENT '渠道来源',
  `last_login_at` datetime DEFAULT NULL COMMENT '最近登录时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='用户表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `user`
--

LOCK TABLES `user` WRITE;
/*!40000 ALTER TABLE `user` DISABLE KEYS */;
INSERT INTO `user` VALUES (1,'d0291907-03f6-4be2-b536-bfbe10a4cfa9','runnergo','','','$2a$10$6Zq1slMtQDrk99RTT/QcqOSprzmBLuGgCHgBXTpYxh0krzoIbBN9W','runnergo','https://apipost.oss-cn-beijing.aliyuncs.com/kunpeng/avatar/default3.png','','','2026-08-29 09:49:22','2026-08-29 08:40:28','2026-08-29 09:49:22',NULL);
/*!40000 ALTER TABLE `user` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `user_collect_info`
--

DROP TABLE IF EXISTS `user_collect_info`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `user_collect_info` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `user_id` varchar(100) NOT NULL COMMENT '用户id',
  `industry` varchar(100) NOT NULL COMMENT '所属行业',
  `team_size` varchar(20) NOT NULL COMMENT '团队规模',
  `work_type` varchar(20) NOT NULL COMMENT '工作岗位',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `user_collect_info`
--

LOCK TABLES `user_collect_info` WRITE;
/*!40000 ALTER TABLE `user_collect_info` DISABLE KEYS */;
/*!40000 ALTER TABLE `user_collect_info` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `user_company`
--

DROP TABLE IF EXISTS `user_company`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `user_company` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `user_id` varchar(100) NOT NULL COMMENT '用户id',
  `company_id` varchar(100) NOT NULL COMMENT '企业id',
  `invite_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '邀请人id',
  `invite_time` datetime DEFAULT NULL COMMENT '邀请时间',
  `status` tinyint(2) unsigned NOT NULL DEFAULT '1' COMMENT '状态：1-正常，2-已禁用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_company_id` (`company_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='用户企业关系表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `user_company`
--

LOCK TABLES `user_company` WRITE;
/*!40000 ALTER TABLE `user_company` DISABLE KEYS */;
INSERT INTO `user_company` VALUES (1,'d0291907-03f6-4be2-b536-bfbe10a4cfa9','12e1c2a9-5637-48af-ad20-00e286fa241f','0','2026-08-29 08:40:29',1,'2026-08-29 08:40:28','2026-08-29 08:40:28',NULL);
/*!40000 ALTER TABLE `user_company` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `user_role`
--

DROP TABLE IF EXISTS `user_role`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `user_role` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `role_id` varchar(100) NOT NULL COMMENT '角色id',
  `user_id` varchar(100) NOT NULL COMMENT '用户id',
  `company_id` varchar(100) NOT NULL DEFAULT '' COMMENT '企业id',
  `team_id` varchar(100) NOT NULL DEFAULT '' COMMENT '团队id',
  `invite_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '邀请人id',
  `invite_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '邀请时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_role_id` (`role_id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COMMENT='用户角色关联表（企业角色、团队角色）';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `user_role`
--

LOCK TABLES `user_role` WRITE;
/*!40000 ALTER TABLE `user_role` DISABLE KEYS */;
INSERT INTO `user_role` VALUES (1,'85967f8f-5bca-4eaf-8766-19f15b6c0536','d0291907-03f6-4be2-b536-bfbe10a4cfa9','12e1c2a9-5637-48af-ad20-00e286fa241f','','0','2026-08-29 08:40:29','2026-08-29 08:40:28','2026-08-29 08:40:28',NULL),(2,'89c0470e-e10a-4e97-8d3f-ff182562b8ad','d0291907-03f6-4be2-b536-bfbe10a4cfa9','','e64b9392-7ab9-4d94-a6d7-00acd96540af','0','2026-08-29 08:56:13','2026-08-29 08:56:13','2026-08-29 08:56:13',NULL);
/*!40000 ALTER TABLE `user_role` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `user_team`
--

DROP TABLE IF EXISTS `user_team`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `user_team` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `user_id` varchar(100) NOT NULL COMMENT '用户ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `role_id` bigint(20) NOT NULL COMMENT '角色id1:超级管理员，2成员，3管理员',
  `team_role_id` varchar(100) NOT NULL DEFAULT '' COMMENT '角色id (角色表对应)',
  `invite_user_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '邀请人id',
  `invite_time` datetime DEFAULT NULL COMMENT '邀请时间',
  `sort` int(11) NOT NULL DEFAULT '0',
  `is_show` tinyint(2) NOT NULL DEFAULT '1' COMMENT '是否展示到团队列表  1:展示   2:不展示',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COMMENT='用户团队关系表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `user_team`
--

LOCK TABLES `user_team` WRITE;
/*!40000 ALTER TABLE `user_team` DISABLE KEYS */;
INSERT INTO `user_team` VALUES (1,'d0291907-03f6-4be2-b536-bfbe10a4cfa9','e64b9392-7ab9-4d94-a6d7-00acd96540af',0,'','0','0000-00-00 00:00:00',0,1,'2026-08-29 08:56:13','2026-08-29 08:56:13',NULL);
/*!40000 ALTER TABLE `user_team` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `user_team_collection`
--

DROP TABLE IF EXISTS `user_team_collection`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `user_team_collection` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT COMMENT '主键id',
  `user_id` varchar(100) NOT NULL COMMENT '用户ID',
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户收藏团队表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `user_team_collection`
--

LOCK TABLES `user_team_collection` WRITE;
/*!40000 ALTER TABLE `user_team_collection` DISABLE KEYS */;
/*!40000 ALTER TABLE `user_team_collection` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `variable`
--

DROP TABLE IF EXISTS `variable`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `variable` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `scene_id` varchar(100) NOT NULL COMMENT '场景ID',
  `type` tinyint(4) NOT NULL DEFAULT '0' COMMENT '使用范围：1-全局变量，2-场景变量',
  `var` varchar(255) NOT NULL COMMENT '变量名',
  `val` text NOT NULL COMMENT '变量值',
  `description` text NOT NULL COMMENT '描述',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '开关状态：1-开启，2-关闭',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`),
  KEY `idx_scene_id` (`scene_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设置变量表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `variable`
--

LOCK TABLES `variable` WRITE;
/*!40000 ALTER TABLE `variable` DISABLE KEYS */;
/*!40000 ALTER TABLE `variable` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `variable_import`
--

DROP TABLE IF EXISTS `variable_import`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `variable_import` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `team_id` varchar(100) NOT NULL COMMENT '团队id',
  `scene_id` varchar(100) NOT NULL DEFAULT '0' COMMENT '场景id',
  `name` varchar(128) NOT NULL COMMENT '文件名称',
  `url` varchar(255) NOT NULL COMMENT '文件地址',
  `uploader_id` varchar(100) NOT NULL COMMENT '上传人id',
  `status` tinyint(2) NOT NULL DEFAULT '1' COMMENT '开关状态：1-开，2-关',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '修改时间',
  `deleted_at` datetime DEFAULT NULL COMMENT '删除时间',
  PRIMARY KEY (`id`),
  KEY `idx_team_id` (`team_id`),
  KEY `idx_scene_id` (`scene_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='导入变量表';
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `variable_import`
--

LOCK TABLES `variable_import` WRITE;
/*!40000 ALTER TABLE `variable_import` DISABLE KEYS */;
/*!40000 ALTER TABLE `variable_import` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Dumping routines for database 'runnergo'
--
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-08-29  1:53:06
