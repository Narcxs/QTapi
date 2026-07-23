-- ============================================================
--  Subscription table for the GexBot indicator.
--  Run this once on your Hostinger MySQL database.
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    email       VARCHAR(255) NOT NULL,
    license_key VARCHAR(64)  NOT NULL UNIQUE,
    status      ENUM('active','inactive','banned') NOT NULL DEFAULT 'inactive',
    expires_at  DATETIME NULL,           -- NULL = never expires
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_license (license_key)
);

-- Demo subscriber (valid for 30 days). Delete before going live.
INSERT INTO users (email, license_key, status, expires_at)
VALUES ('demo@example.com', 'DEMO-KEY-123', 'active',
        DATE_ADD(NOW(), INTERVAL 30 DAY));
