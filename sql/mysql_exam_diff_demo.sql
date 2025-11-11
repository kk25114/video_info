-- MySQL 8+ demo: compare base code table across exam_id versions
-- Run this script top-to-bottom to create schema, seed data, and query diffs.

-- 0) Clean start (idempotent)
DROP DATABASE IF EXISTS demo_exam;
CREATE DATABASE demo_exam CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE demo_exam;

-- 1) Table definition
CREATE TABLE base_code (
  exam_id   INT NOT NULL,
  code      VARCHAR(32) NOT NULL,
  name      VARCHAR(100) NULL,
  category  VARCHAR(50)  NULL,
  status    VARCHAR(16)  NULL, -- e.g., 'active'/'inactive'
  note      VARCHAR(200) NULL,
  PRIMARY KEY (exam_id, code),
  KEY idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2) Seed sample data
-- Old exam: 2023
INSERT INTO base_code (exam_id, code, name, category, status, note) VALUES
  (2023, 'A001', 'Alpha',       'Cat1', 'active',   NULL),
  (2023, 'A002', 'Beta',        'Cat1', 'inactive', NULL),
  (2023, 'A003', 'Gamma',       'Cat2', 'active',   'Legacy'),
  (2023, 'A004', 'Delta',       'Cat2', NULL,       NULL);

-- New exam: 2024
INSERT INTO base_code (exam_id, code, name, category, status, note) VALUES
  (2024, 'A001', 'Alpha Prime', 'Cat1', 'active',   NULL), -- modified name
  (2024, 'A003', 'gamma',       '  Cat2  ', 'active', 'Legacy'), -- case/space only
  (2024, 'A004', 'Delta',       'Cat2', 'active',   NULL), -- NULL -> active
  (2024, 'A005', 'Epsilon',     'Cat1', 'active',   NULL), -- added
  (2024, 'A006', 'Eta',         'Cat3', 'inactive', NULL); -- added

-- 3) Exact diff between two exam_ids (treat case/space differences as changes)
-- Parameters (set as needed)
SET @old_id = 2023;
SET @new_id = 2024;

WITH
old_data AS (
  SELECT code, name, category, status, note
  FROM base_code
  WHERE exam_id = @old_id
),
new_data AS (
  SELECT code, name, category, status, note
  FROM base_code
  WHERE exam_id = @new_id
)
-- Added
SELECT n.code,
       'added' AS change_type,
       NULL AS old_name, NULL AS old_category, NULL AS old_status, NULL AS old_note,
       n.name AS new_name, n.category AS new_category, n.status AS new_status, n.note AS new_note,
       NULL AS changed_cols
FROM new_data n
LEFT JOIN old_data o ON o.code = n.code
WHERE o.code IS NULL

UNION ALL
-- Removed
SELECT o.code,
       'removed' AS change_type,
       o.name, o.category, o.status, o.note,
       NULL, NULL, NULL, NULL,
       NULL AS changed_cols
FROM old_data o
LEFT JOIN new_data n ON n.code = o.code
WHERE n.code IS NULL

UNION ALL
-- Modified (any of the compared fields differ; NULL-safe)
SELECT n.code,
       'modified' AS change_type,
       o.name, o.category, o.status, o.note,
       n.name, n.category, n.status, n.note,
       TRIM(BOTH ',' FROM CONCAT_WS(',',
         CASE WHEN NOT (n.name     <=> o.name)     THEN 'name'     END,
         CASE WHEN NOT (n.category <=> o.category) THEN 'category' END,
         CASE WHEN NOT (n.status   <=> o.status)   THEN 'status'   END,
         CASE WHEN NOT (n.note     <=> o.note)     THEN 'note'     END
       )) AS changed_cols
FROM new_data n
JOIN old_data o ON o.code = n.code
WHERE NOT (n.name     <=> o.name)
   OR NOT (n.category <=> o.category)
   OR NOT (n.status   <=> o.status)
   OR NOT (n.note     <=> o.note)
ORDER BY change_type, code;

-- 4) Summary counts by change type (exact)
WITH
old_data AS (
  SELECT code, name, category, status, note FROM base_code WHERE exam_id = @old_id
),
new_data AS (
  SELECT code, name, category, status, note FROM base_code WHERE exam_id = @new_id
),
diff AS (
  SELECT 'added' AS change_type FROM new_data n LEFT JOIN old_data o ON o.code = n.code WHERE o.code IS NULL
  UNION ALL
  SELECT 'removed' FROM old_data o LEFT JOIN new_data n ON n.code = o.code WHERE n.code IS NULL
  UNION ALL
  SELECT 'modified' FROM new_data n JOIN old_data o ON o.code = n.code
   WHERE NOT (n.name <=> o.name) OR NOT (n.category <=> o.category) OR NOT (n.status <=> o.status) OR NOT (n.note <=> o.note)
)
SELECT change_type, COUNT(*) AS cnt
FROM diff
GROUP BY change_type
ORDER BY change_type;

-- 5) Normalized diff: ignore case and surrounding spaces on text fields
WITH
old_data AS (
  SELECT code,
         NULLIF(LOWER(TRIM(name)), '')     AS name,
         NULLIF(LOWER(TRIM(category)), '') AS category,
         status,
         NULLIF(TRIM(note), '')            AS note
  FROM base_code
  WHERE exam_id = @old_id
),
new_data AS (
  SELECT code,
         NULLIF(LOWER(TRIM(name)), '')     AS name,
         NULLIF(LOWER(TRIM(category)), '') AS category,
         status,
         NULLIF(TRIM(note), '')            AS note
  FROM base_code
  WHERE exam_id = @new_id
)
-- Added
SELECT n.code,
       'added' AS change_type,
       NULL AS old_name, NULL AS old_category, NULL AS old_status, NULL AS old_note,
       n.name AS new_name, n.category AS new_category, n.status AS new_status, n.note AS new_note,
       NULL AS changed_cols
FROM new_data n
LEFT JOIN old_data o ON o.code = n.code
WHERE o.code IS NULL

UNION ALL
-- Removed
SELECT o.code,
       'removed' AS change_type,
       o.name, o.category, o.status, o.note,
       NULL, NULL, NULL, NULL,
       NULL AS changed_cols
FROM old_data o
LEFT JOIN new_data n ON n.code = o.code
WHERE n.code IS NULL

UNION ALL
-- Modified (normalized)
SELECT n.code,
       'modified' AS change_type,
       o.name, o.category, o.status, o.note,
       n.name, n.category, n.status, n.note,
       TRIM(BOTH ',' FROM CONCAT_WS(',',
         CASE WHEN NOT (n.name     <=> o.name)     THEN 'name'     END,
         CASE WHEN NOT (n.category <=> o.category) THEN 'category' END,
         CASE WHEN NOT (n.status   <=> o.status)   THEN 'status'   END,
         CASE WHEN NOT (n.note     <=> o.note)     THEN 'note'     END
       )) AS changed_cols
FROM new_data n
JOIN old_data o ON o.code = n.code
WHERE NOT (n.name     <=> o.name)
   OR NOT (n.category <=> o.category)
   OR NOT (n.status   <=> o.status)
   OR NOT (n.note     <=> o.note)
ORDER BY change_type, code;

-- 6) Reusable stored procedures
DROP PROCEDURE IF EXISTS sp_exam_diff;
DELIMITER $$
CREATE PROCEDURE sp_exam_diff(IN p_old_id INT, IN p_new_id INT)
BEGIN
  WITH old_data AS (
         SELECT code, name, category, status, note
         FROM base_code WHERE exam_id = p_old_id
       ),
       new_data AS (
         SELECT code, name, category, status, note
         FROM base_code WHERE exam_id = p_new_id
       )
  SELECT * FROM (
    SELECT n.code,
           'added' AS change_type,
           NULL AS old_name, NULL AS old_category, NULL AS old_status, NULL AS old_note,
           n.name AS new_name, n.category AS new_category, n.status AS new_status, n.note AS new_note,
           NULL AS changed_cols
    FROM new_data n
    LEFT JOIN old_data o ON o.code = n.code
    WHERE o.code IS NULL

    UNION ALL

    SELECT o.code,
           'removed' AS change_type,
           o.name, o.category, o.status, o.note,
           NULL, NULL, NULL, NULL,
           NULL AS changed_cols
    FROM old_data o
    LEFT JOIN new_data n ON n.code = o.code
    WHERE n.code IS NULL

    UNION ALL

    SELECT n.code,
           'modified' AS change_type,
           o.name, o.category, o.status, o.note,
           n.name, n.category, n.status, n.note,
           TRIM(BOTH ',' FROM CONCAT_WS(',',
             CASE WHEN NOT (n.name     <=> o.name)     THEN 'name'     END,
             CASE WHEN NOT (n.category <=> o.category) THEN 'category' END,
             CASE WHEN NOT (n.status   <=> o.status)   THEN 'status'   END,
             CASE WHEN NOT (n.note     <=> o.note)     THEN 'note'     END
           )) AS changed_cols
    FROM new_data n
    JOIN old_data o ON o.code = n.code
    WHERE NOT (n.name     <=> o.name)
       OR NOT (n.category <=> o.category)
       OR NOT (n.status   <=> o.status)
       OR NOT (n.note     <=> o.note)
  ) d
  ORDER BY change_type, code;
END $$
DELIMITER ;

DROP PROCEDURE IF EXISTS sp_exam_diff_normalized;
DELIMITER $$
CREATE PROCEDURE sp_exam_diff_normalized(IN p_old_id INT, IN p_new_id INT)
BEGIN
  WITH old_data AS (
         SELECT code,
                NULLIF(LOWER(TRIM(name)), '')     AS name,
                NULLIF(LOWER(TRIM(category)), '') AS category,
                status,
                NULLIF(TRIM(note), '')            AS note
         FROM base_code WHERE exam_id = p_old_id
       ),
       new_data AS (
         SELECT code,
                NULLIF(LOWER(TRIM(name)), '')     AS name,
                NULLIF(LOWER(TRIM(category)), '') AS category,
                status,
                NULLIF(TRIM(note), '')            AS note
         FROM base_code WHERE exam_id = p_new_id
       )
  SELECT * FROM (
    SELECT n.code,
           'added' AS change_type,
           NULL AS old_name, NULL AS old_category, NULL AS old_status, NULL AS old_note,
           n.name AS new_name, n.category AS new_category, n.status AS new_status, n.note AS new_note,
           NULL AS changed_cols
    FROM new_data n
    LEFT JOIN old_data o ON o.code = n.code
    WHERE o.code IS NULL

    UNION ALL

    SELECT o.code,
           'removed' AS change_type,
           o.name, o.category, o.status, o.note,
           NULL, NULL, NULL, NULL,
           NULL AS changed_cols
    FROM old_data o
    LEFT JOIN new_data n ON n.code = o.code
    WHERE n.code IS NULL

    UNION ALL

    SELECT n.code,
           'modified' AS change_type,
           o.name, o.category, o.status, o.note,
           n.name, n.category, n.status, n.note,
           TRIM(BOTH ',' FROM CONCAT_WS(',',
             CASE WHEN NOT (n.name     <=> o.name)     THEN 'name'     END,
             CASE WHEN NOT (n.category <=> o.category) THEN 'category' END,
             CASE WHEN NOT (n.status   <=> o.status)   THEN 'status'   END,
             CASE WHEN NOT (n.note     <=> o.note)     THEN 'note'     END
           )) AS changed_cols
    FROM new_data n
    JOIN old_data o ON o.code = n.code
    WHERE NOT (n.name     <=> o.name)
       OR NOT (n.category <=> o.category)
       OR NOT (n.status   <=> o.status)
       OR NOT (n.note     <=> o.note)
  ) d
  ORDER BY change_type, code;
END $$
DELIMITER ;

-- 7) Example calls
-- Exact comparison
CALL sp_exam_diff(2023, 2024);

-- Normalized comparison (ignores case/space in name/category, trims note)
CALL sp_exam_diff_normalized(2023, 2024);

-- 8) Example: summary counts via procedure result (client-side aggregate) –
-- run the SELECT from section 4 if you need aggregated counts server-side.

