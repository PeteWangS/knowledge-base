# Databricks Unity Catalog 基于属性的访问控制 (ABAC) 完整技术指南

> **核心定义**：ABAC（Attribute-Based Access Control，基于属性的访问控制）是 Databricks Unity Catalog 提供的现代数据安全治理范式。与传统的 RBAC（基于角色逐表手工授权）不同，ABAC 的核心哲学是 **“打标即治理（Tag-driven Governance）”** —— 策略一次定义、全局生效；日常运维只需对数据对象（Catalog / Schema / Table / Column）打上对应的 Tag（标签），系统即在查询编译期自动应用行级过滤、动态脱敏或访问权限控制。

---

## 一、官方核心文档导航

* [Unity Catalog ABAC 核心概念（微软官方文档）](https://learn.microsoft.com/zh-cn/azure/databricks/data-governance/unity-catalog/abac/core-concepts)
* [Unity Catalog 行过滤与列掩码（行级安全与动态脱敏）](https://learn.microsoft.com/zh-cn/azure/databricks/data-governance/unity-catalog/filter)
* [Unity Catalog 权限与可安全保护对象模型](https://learn.microsoft.com/zh-cn/azure/databricks/data-governance/unity-catalog/manage-privileges/)
* [CREATE POLICY 语法参考](https://learn.microsoft.com/zh-cn/azure/databricks/sql/language-manual/sql-ref-syntax-ddl-create-policy)

---

## 二、ABAC 策略四大核心类型

在 Unity Catalog ABAC 治理体系中，核心支持以下四类策略，覆盖了从“粗粒度对象进门许可”到“细粒度动态数据塑形”的完整生命周期：

| 策略类别 | 策略关键字 | 核心作用与底层行为 | 核心特征 / 返回值要求 |
| :--- | :--- | :--- | :--- |
| **粗粒度对象访问** | **`GRANT 策略`** | 当数据对象命中 Tag 条件时，**自动增量追加**访问权限（例如打上 `public` 的表自动授予所有人 `SELECT`）。 | **纯加法（Additive Only）**：只能增发钥匙，无法撤销已有的直接授权。 |
| | **`DENY 策略`** | 当数据对象或操作命中 Tag 条件时，**显式一票否决**访问权限。 | **最高优先级**：`DENY > DIRECT GRANT > INHERITED GRANT`，即使是 Owner 或被直接 GRANT，命中也强制拦截。 |
| **细粒度数据保护** | **`ROW FILTER 策略`** | 控制用户**“能看到哪些行”**（行级隔离）。底层自动重写为 `WHERE func(...) = TRUE`。 | 绑定的 UDF **必须返回 `BOOLEAN`**。传参使用 `USING COLUMNS (...)`。 |
| | **`COLUMN MASK 策略`**| 控制用户**“某列看到什么样”**（动态数据脱敏）。底层自动重写为 `SELECT func(...) AS col`。 | 绑定的 UDF **返回值类型必须与原列兼容**。使用 `ON COLUMN` 锚定脱敏目标。 |

---

## 三、策略的三层结构模型

一条标准的 Unity Catalog 安全策略可以清晰地划分为三大部分：

```
┌────────────────────────────────────────────────────────┐
│ 1. 数据转换/过滤算法 (Function / UDF)                     │
│    -> 纯业务逻辑，决定"怎么脱敏"或"怎么过滤"              │
└────────────────────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ 2. 策略框架与权限主体 (Policy Header & Principals)       │
│    -> 指定作用域 (Scope)、绑定动作与函数、白名单与受限人群 │
└────────────────────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ 3. 动态匹配规则与应用目标 (Target Matching Rules)          │
│    -> WHEN 表条件、MATCH COLUMNS 列匹配、ON/USING 传参绑定 │
└────────────────────────────────────────────────────────┘
```

### 1. 第一部分：算法逻辑实现（Function 定义）
与具体表和列彻底解耦的通用 SQL 标量函数：
```sql
-- 脱敏函数示例
CREATE FUNCTION mask_pii(val STRING) RETURNS STRING
  RETURN '***';

-- 行过滤函数示例（必须返回 BOOLEAN）
CREATE FUNCTION filter_by_region(region STRING, allowed STRING) RETURNS BOOLEAN
  RETURN region = allowed;
```

### 2. 第二部分：策略框架与主体（Policy Header & Principals）
明确管辖边界、绑定算子与受众群体：
```sql
CREATE POLICY mask_pii_for_hr
ON CATALOG catalog_a                        -- 作用域：可为 Metastore / Catalog / Schema
COLUMN MASK mask_pii                        -- 绑定的脱敏动作与函数
TO `account users` EXCEPT `HR admins`       -- 生效受众及豁免白名单 (RBAC 结合)
```

### 3. 第三部分：属性匹配与参数绑定（ABAC 核心规则）
自动捕获符合条件的表与列：
```sql
FOR TABLES
WHEN has_tag('HR')                          -- 容器级过滤（表标签）
MATCH COLUMNS has_tag('PII') AS pii_col     -- 属性级匹配（字段标签）
ON COLUMN pii_col;                          -- 目标落地点与参数传递
```

---

## 四、深入解析：列脱敏（Column Masking）实战

### 1. 单列脱敏与隐式绑定语法糖
```sql
MATCH COLUMNS has_tag('PII') AS pii_col
ON COLUMN pii_col;
```
* **原理解析**：`ON COLUMN pii_col` 实际上是语法糖，完整等价形式为 `ON COLUMN pii_col(pii_col)`。
* 引擎自动将匹配到的 `pii_col` 本身作为脱敏函数的**第 1 个入参**，并将函数输出覆盖回 `pii_col`。

### 2. 批量多列自动脱敏（“打标即生效”）
* **`MATCH COLUMNS` 在底层是集合匹配逻辑**，而不是只匹配一个列。
* 若表中有姓名、电话、身份证等多个列需要脱敏，**策略 SQL 无需修改**，只需给这些字段都打上 `PII` 标签：
  ```sql
  ALTER TABLE catalog_a.schema_hr.employees ALTER COLUMN user_name SET TAGS ('PII' = 'true');
  ALTER TABLE catalog_a.schema_hr.employees ALTER COLUMN address SET TAGS ('PII' = 'true');
  ALTER TABLE catalog_a.schema_hr.employees ALTER COLUMN id_card SET TAGS ('PII' = 'true');
  ```
* 引擎在执行查询时，会自动对每一个匹配到的字段展开应用脱敏函数，零额外维护成本。

### 3. 跨列引用与附加传参（Cross-column Reference）
在对“A 列”做脱敏时，需要参考“B 列”的值（例如根据部门、国别决定是否脱敏）：

```sql
-- 1. 定义多参数脱敏函数
CREATE FUNCTION mask_ssn(ssn STRING, show_last INT) RETURNS STRING
  RETURN CONCAT('***-**-', RIGHT(ssn, show_last));

-- 2. 创建策略：目标列 + 附加常量/跨列参数
CREATE POLICY mask_ssn_columns
ON CATALOG hr_catalog
COLUMN MASK mask_ssn
TO `account users` EXCEPT `compliance team`
FOR TABLES
MATCH COLUMNS has_tag_value('pii', 'ssn') AS ssn_col
ON COLUMN ssn_col              -- 第 1 参数自动绑定 ssn_col
USING COLUMNS (4);             -- 第 2 参数通过 USING COLUMNS 显式传入常量 4
```

---

## 五、深入解析：行级过滤（Row Filtering）实战

行过滤解决的是**多租户隔离与数据可见范围**问题。

```sql
-- 1. 过滤函数：必须返回布尔值
CREATE FUNCTION filter_by_region(region STRING, allowed STRING) RETURNS BOOLEAN
  RETURN region = allowed;

-- 2. 行过滤策略
CREATE POLICY regional_access_emea
ON CATALOG sales
ROW FILTER filter_by_region
TO `emea team`
FOR TABLES
MATCH COLUMNS has_tag('region') AS rgn
USING COLUMNS (rgn, 'EMEA');
```

* **执行行为**：当 `emea team` 的用户查询 `sales` 下的任意表时，引擎底层自动追加：
  ```sql
  WHERE filter_by_region(rgn, 'EMEA')
  -- 等价于 WHERE rgn = 'EMEA'
  ```
* **语法差异注意**：
  * **`COLUMN MASK`** 必须有 `ON COLUMN target_col`，因为必须指定“哪一列被替换遮盖”；
  * **`ROW FILTER`** **没有** `ON COLUMN`，因为影响的是整行记录的可见性，全部参数统一由 `USING COLUMNS (...)` 按序供给。

---

## 六、关键语法对比：ON COLUMN vs USING COLUMNS

| 对比维度 | `ON COLUMN A(A, B)` (紧凑写法) | `ON COLUMN A USING COLUMNS (B)` (现行标准) | `USING COLUMNS (A, B)` (行过滤标准) |
| :--- | :--- | :--- | :--- |
| **所属策略类型** | `COLUMN MASK` | `COLUMN MASK` | `ROW FILTER` |
| **目标列锚定** | 锚定到 A 列 | 锚定到 A 列（A 自动绑定第 1 参数） | **无目标列**（整行过滤，只管喂参） |
| **能否传递常量** | ❌ **不支持**（括号内只能写列别名） | ✅ **支持**（如 `USING COLUMNS (4)`） | ✅ **支持**（如 `USING COLUMNS (rgn, 'EMEA')`） |
| **标签反省函数** | ❌ 不支持 | ✅ **支持**（如 `tag_value(...)`） | ✅ **支持** |
| **官方推荐度** | 兼容早期语法 | **官方强烈推荐（职责分离清晰）** | **官方标准规范** |

---

## 七、权限计算机制：GRANT 仅做加法 vs DENY 一票否决

### 1. 为什么“GRANT 策略仅添加访问权限，无法撤销直接授予的访问权限”？
在 Unity Catalog 的权限解析链中，常规权限（GRANT）在底层是按**并集（OR / Union）**逻辑合并的：

$$\text{最终有效权限} = \text{直接授予权限 (Direct Grant)} \cup \text{继承组权限} \cup \text{ABAC GRANT 策略增量}$$

* **GRANT 策略的作用是“发新钥匙”**。
* 如果某用户之前已被管理员直接授予了 `GRANT SELECT ON TABLE t TO user;`，那么即便他不满足 ABAC GRANT 策略的打标规则，他凭借手头的“老钥匙”依然可以访问。
* **GRANT 策略无法做“减法”**。

### 2. 如何实现硬性拦截与剥夺？
1. **显式撤销**：通过 `REVOKE SELECT ...` 彻底清理历史上的直接权限。
2. **使用 DENY 策略**：
   ```sql
   CREATE POLICY deny_confidential_access
   ON CATALOG hr_catalog
   DENY SELECT
   TO `contractors`
   FOR TABLES WHEN has_tag('confidential');
   ```
   * **DENY 优先级最高**：在权限判定中，`DENY` 具有绝对的一票否决权。只要命中 DENY 策略，无论该用户身上有多少直接授权或继承权限，都会被强制拦截。

---

## 八、高级进阶与限制突破

### 1. `WHEN` 子句复合条件
`WHEN` 表级条件是标准布尔表达式，支持使用 `AND`、`OR`、`NOT` 进行无限自由组合：
```sql
FOR TABLES
WHEN (has_tag('HR') OR has_tag('FINANCE')) 
 AND has_tag_value('env', 'prod') 
 AND NOT has_tag('test')
```

### 2. `MATCH COLUMNS` 列数量上限突破方案
* **官方硬性限制**：
  * **行过滤（ROW FILTER）** 最多匹配 **6 个** 列表达式；
  * **列掩码（COLUMN MASK）** 最多匹配 **3 个** 列表达式（目标列 + 最多 2 个参考列）。

如果判定逻辑极其复杂，需要参考 7 个甚至更多列，推荐采用以下架构方案：

#### 方案 A：拆解为多条策略链（Policy Chaining - 适用于行过滤）
* 将 8 个条件的行过滤拆分成两条独立的 Policy（各检查 4 列）。
* Unity Catalog 在底层会自动将它们用 `AND` 逻辑串联生效：
  $$\text{WHERE Policy\_1(c1..c4) AND Policy\_2(c5..c8)}$$

#### 方案 B：使用 `STRUCT` 复合结构打包多列（最优雅通用）
策略引擎计算列配额时，**仅按物理顶层列（Top-level Column）计算**。一个 `STRUCT` 无论包含多少个子字段，在策略引擎看来**都只占用 1 个列配额**！

```sql
-- 1. 创建接收 STRUCT 的 UDF
CREATE FUNCTION mask_salary_complex(
    sal DOUBLE, 
    ctx STRUCT<dept: STRING, level: INT, country: STRING, emp_type: STRING>
) RETURNS DOUBLE
  RETURN CASE 
    WHEN ctx.dept = 'HR' AND ctx.level >= 8 THEN sal
    WHEN ctx.country = 'US' AND ctx.emp_type = 'FTE' THEN sal
    ELSE 0.0
  END;

-- 2. 策略仅占用 2 个列配额（sal_col + ctx_col <= 3）
CREATE POLICY mask_salary_struct_policy
ON CATALOG hr_catalog
COLUMN MASK mask_salary_complex
TO `account users` EXCEPT `hr_super_admin`
FOR TABLES
MATCH COLUMNS 
    has_tag('pii_salary') AS sal_col,
    has_tag('security_ctx') AS ctx_col
ON COLUMN sal_col
USING COLUMNS (ctx_col);
```

#### 常见疑问：已有历史表如何使用 STRUCT？
无需推倒重来重建物理表：
* **方式一：通过 View 视图打包（推荐，零数据重写）**
  ```sql
  CREATE VIEW hr_catalog.schema_hr.employees AS
  SELECT 
      emp_id, salary,
      named_struct('dept', dept, 'level', level, 'country', country, 'emp_type', emp_type) AS auth_ctx
  FROM hr_catalog.schema_hr.employees_raw;
  ```
* **方式二：在现有 Delta 表上追加生成列（Generated Column）**
  ```sql
  ALTER TABLE hr_catalog.schema_hr.employees 
  ADD COLUMN auth_ctx STRUCT<dept: STRING, level: INT, country: STRING, emp_type: STRING>
  GENERATED ALWAYS AS (
      named_struct('dept', dept, 'level', level, 'country', country, 'emp_type', emp_type)
  );
  ```

---

## 九、运行环境与模式要求

| 检查项 | 推荐配置 / 要求 | 检查方式 / 命令 |
| :--- | :--- | :--- |
| **Databricks Runtime (DBR)** | **DBR 15.4 LTS 或更高版本**（推荐 16.0+）；或 SQL Warehouse (Current 通道) | Python: `os.environ.get("DATABRICKS_RUNTIME_VERSION")`<br>SQL: `SELECT current_version();` |
| **计算引擎访问模式** | **`Shared`（标准共享 / Standard）** 或 **`Single User`（单用户）**；必须受 Unity Catalog 托管 | Python: `spark.conf.get("spark.databricks.clusterUsageTags.dataSecurityMode")`<br>*(返回 `USER_ISOLATION` 或 `SINGLE_USER`)* |
| **UDF 语言选型** | **强烈推荐 SQL UDF** | SQL UDF 支持 Catalyst 优化器内联代码生成；避免在行/列过滤中使用 Python UDF（会引起跨进程 IPC 序列化性能瓶颈）。 |
