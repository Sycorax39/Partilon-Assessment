-- Demo data.
--   O1001            : C001's oldest order (DELIVERED)  -> "What is the status of order O1001?"
--   O1006            : C001's LATEST order (SHIPPED)    -> "C001 and their latest order status"
--   C004             : has no orders                    -> "no orders found" handling
--   C999             : does not exist anywhere

INSERT INTO orders (order_id, customer_id, status, currency, total_amount, created_at, updated_at) VALUES
    ('O1001', 'C001', 'DELIVERED', 'THB', 1880.00, '2026-08-14T10:20:00+07:00', '2026-08-18T15:02:00+07:00'),
    ('O1002', 'C002', 'PAID',      'THB', 3490.00, '2026-09-02T19:45:00+07:00', '2026-09-02T19:47:00+07:00'),
    ('O1003', 'C001', 'CANCELLED', 'THB',  890.00, '2026-09-10T08:05:00+07:00', '2026-09-10T12:30:00+07:00'),
    ('O1004', 'C003', 'DELIVERED', 'THB', 8990.00, '2026-09-15T14:00:00+07:00', '2026-09-19T11:10:00+07:00'),
    ('O1005', 'C005', 'PENDING',   'THB',  900.00, '2026-10-01T21:30:00+07:00', '2026-10-01T21:30:00+07:00'),
    ('O1006', 'C001', 'SHIPPED',   'THB', 5480.00, '2026-10-03T11:15:00+07:00', '2026-10-05T09:40:00+07:00'),
    ('O1007', 'C002', 'PENDING',   'THB', 1890.00, '2026-10-05T17:25:00+07:00', '2026-10-05T17:25:00+07:00')
ON CONFLICT DO NOTHING;

INSERT INTO order_items (order_id, line_no, sku, product_name, quantity, unit_price) VALUES
    ('O1001', 1, 'MSE-WL-01',  'Wireless Mouse',              1,  590.00),
    ('O1001', 2, 'HUB-USBC-7', 'USB-C 7-in-1 Hub',            1, 1290.00),
    ('O1002', 1, 'KBD-MECH-87','Mechanical Keyboard (TKL)',   1, 3490.00),
    ('O1003', 1, 'STD-MON-01', 'Monitor Stand',               1,  890.00),
    ('O1004', 1, 'MON-27-QHD', '27" QHD Monitor',             1, 8990.00),
    ('O1005', 1, 'SLV-LAP-14', 'Laptop Sleeve 14"',           2,  450.00),
    ('O1006', 1, 'HPH-ANC-02', 'Noise-cancelling Headphones', 1, 4990.00),
    ('O1006', 2, 'CBL-USBC-2M','USB-C Cable 2m',              1,  490.00),
    ('O1007', 1, 'CAM-1080-01','1080p Webcam',                1, 1890.00)
ON CONFLICT DO NOTHING;

SELECT setval('order_number_seq',
              GREATEST((SELECT COALESCE(MAX(SUBSTRING(order_id FROM 2)::int), 1000) FROM orders), 1000));
