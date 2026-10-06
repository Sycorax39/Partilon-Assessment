-- Demo data. C001 is the main demo customer. C004 has no orders. C999 deliberately does not exist.

INSERT INTO customers (customer_id, name, email, phone, tier, created_at, updated_at) VALUES
    ('C001', 'Somchai Jaidee',    'somchai.j@example.com',   '+66812345678', 'GOLD',     '2024-03-12T09:15:00+07:00', '2025-11-02T14:20:00+07:00'),
    ('C002', 'Anong Srisuk',      'anong.s@example.com',     '+66823456789', 'STANDARD', '2024-07-01T10:00:00+07:00', '2024-07-01T10:00:00+07:00'),
    ('C003', 'Kittipong Wongsa',  'kittipong.w@example.com', '+66834567890', 'PLATINUM', '2023-11-20T16:45:00+07:00', '2026-01-15T11:05:00+07:00'),
    ('C004', 'Malee Chaiyaporn',  'malee.c@example.com',     NULL,           'STANDARD', '2025-02-08T08:30:00+07:00', '2025-02-08T08:30:00+07:00'),
    ('C005', 'Niran Boonmee',     'niran.b@example.com',     '+66856789012', 'STANDARD', '2025-09-30T13:10:00+07:00', '2025-09-30T13:10:00+07:00')
ON CONFLICT DO NOTHING;

-- Keep the ID sequence ahead of the seeded IDs so new customers get C006, C007, ...
SELECT setval('customer_number_seq',
              GREATEST((SELECT COALESCE(MAX(SUBSTRING(customer_id FROM 2)::int), 0) FROM customers), 1));
