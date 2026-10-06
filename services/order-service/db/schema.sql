-- Order Service schema. Idempotent: safe to run on every start.
-- Note: customer_id is NOT a foreign key — customers live in a different service and database.

CREATE SEQUENCE IF NOT EXISTS order_number_seq START 1001;

CREATE TABLE IF NOT EXISTS orders (
    order_id     VARCHAR(12)   PRIMARY KEY DEFAULT ('O' || nextval('order_number_seq')::text),
    customer_id  VARCHAR(10)   NOT NULL,
    status       VARCHAR(12)   NOT NULL
                 CHECK (status IN ('PENDING', 'PAID', 'SHIPPED', 'DELIVERED', 'CANCELLED')),
    currency     CHAR(3)       NOT NULL DEFAULT 'THB',
    total_amount NUMERIC(12,2) NOT NULL CHECK (total_amount >= 0),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Supports "latest order for customer X" without a full scan.
CREATE INDEX IF NOT EXISTS idx_orders_customer_created ON orders (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS order_items (
    order_id     VARCHAR(12)   NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    line_no      INT           NOT NULL,
    sku          VARCHAR(40)   NOT NULL,
    product_name VARCHAR(120)  NOT NULL,
    quantity     INT           NOT NULL CHECK (quantity > 0),
    unit_price   NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (order_id, line_no)
);
