-- Customer Service schema. Idempotent: safe to run on every start.

CREATE SEQUENCE IF NOT EXISTS customer_number_seq START 1;

CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR(10)  PRIMARY KEY
                DEFAULT ('C' || LPAD(nextval('customer_number_seq')::text, 3, '0')),
    name        VARCHAR(100) NOT NULL,
    email       VARCHAR(254) NOT NULL UNIQUE,
    phone       VARCHAR(20),
    tier        VARCHAR(10)  NOT NULL DEFAULT 'STANDARD'
                CHECK (tier IN ('STANDARD', 'GOLD', 'PLATINUM')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
