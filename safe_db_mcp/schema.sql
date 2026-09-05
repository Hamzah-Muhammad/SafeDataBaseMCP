-- Seed schema for the SafeDataBaseMCP demo database: a small public library.
-- Rebuilt from scratch whenever the database file is missing, so the demo is
-- always reproducible from a clean clone.

PRAGMA foreign_keys = ON;

CREATE TABLE authors (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    birth_year  INTEGER,
    country     TEXT    NOT NULL
);

CREATE TABLE books (
    id              INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL,
    author_id       INTEGER NOT NULL REFERENCES authors(id),
    isbn            TEXT    NOT NULL UNIQUE,
    published_year  INTEGER NOT NULL,
    shelf           TEXT    NOT NULL,
    copies_total    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE members (
    id         INTEGER PRIMARY KEY,
    full_name  TEXT NOT NULL,
    email      TEXT NOT NULL UNIQUE,
    joined_on  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE loans (
    id           INTEGER PRIMARY KEY,
    book_id      INTEGER NOT NULL REFERENCES books(id),
    member_id    INTEGER NOT NULL REFERENCES members(id),
    borrowed_on  TEXT    NOT NULL,
    due_on       TEXT    NOT NULL,
    returned_on  TEXT
);

CREATE INDEX idx_books_author ON books(author_id);
CREATE INDEX idx_loans_member ON loans(member_id);
CREATE INDEX idx_loans_book   ON loans(book_id);

INSERT INTO authors (id, name, birth_year, country) VALUES
    (1, 'Ursula K. Le Guin',   1929, 'United States'),
    (2, 'Chinua Achebe',       1930, 'Nigeria'),
    (3, 'Italo Calvino',       1923, 'Italy'),
    (4, 'Octavia E. Butler',   1947, 'United States'),
    (5, 'Kazuo Ishiguro',      1954, 'United Kingdom'),
    (6, 'Toni Morrison',       1931, 'United States');

INSERT INTO books (id, title, author_id, isbn, published_year, shelf, copies_total) VALUES
    (1, 'A Wizard of Earthsea',            1, '978-0553262506', 1968, 'SF-A-01', 4),
    (2, 'The Left Hand of Darkness',       1, '978-0441478125', 1969, 'SF-A-02', 3),
    (3, 'The Dispossessed',                1, '978-0060512750', 1974, 'SF-A-03', 2),
    (4, 'Things Fall Apart',               2, '978-0385474542', 1958, 'LIT-B-11', 5),
    (5, 'Arrow of God',                    2, '978-0385014809', 1964, 'LIT-B-12', 2),
    (6, 'Invisible Cities',                3, '978-0156453806', 1972, 'LIT-C-04', 3),
    (7, 'If on a Winters Night a Traveler', 3, '978-0156439619', 1979, 'LIT-C-05', 2),
    (8, 'Kindred',                         4, '978-0807083697', 1979, 'SF-D-07', 4),
    (9, 'Parable of the Sower',            4, '978-1538732182', 1993, 'SF-D-08', 3),
    (10, 'The Remains of the Day',         5, '978-0679731726', 1989, 'LIT-E-02', 2),
    (11, 'Never Let Me Go',                5, '978-1400078776', 2005, 'LIT-E-03', 4),
    (12, 'Beloved',                        6, '978-1400033416', 1987, 'LIT-F-09', 3),
    (13, 'Song of Solomon',                6, '978-1400033423', 1977, 'LIT-F-10', 2);

INSERT INTO members (id, full_name, email, joined_on, status) VALUES
    (1, 'Amara Osei',      'amara.osei@example.com',      '2023-01-14', 'active'),
    (2, 'Daniel Whitfield', 'd.whitfield@example.com',    '2023-03-02', 'active'),
    (3, 'Priya Raman',     'priya.raman@example.com',     '2023-06-19', 'active'),
    (4, 'Lukas Vogel',     'lukas.vogel@example.com',     '2024-02-08', 'suspended'),
    (5, 'Mei Tanaka',      'mei.tanaka@example.com',      '2024-05-27', 'active'),
    (6, 'Sofia Marchetti', 'sofia.marchetti@example.com', '2024-09-11', 'active'),
    (7, 'Owen Brady',      'owen.brady@example.com',      '2025-01-30', 'active'),
    (8, 'Nadia Haddad',    'nadia.haddad@example.com',    '2025-04-16', 'lapsed');

INSERT INTO loans (id, book_id, member_id, borrowed_on, due_on, returned_on) VALUES
    (1,  1,  1, '2025-11-03', '2025-11-24', '2025-11-19'),
    (2,  4,  2, '2025-11-05', '2025-11-26', '2025-11-25'),
    (3,  8,  3, '2025-11-12', '2025-12-03', NULL),
    (4,  2,  1, '2025-11-20', '2025-12-11', NULL),
    (5, 12,  5, '2025-11-22', '2025-12-13', '2025-12-01'),
    (6,  6,  6, '2025-12-01', '2025-12-22', NULL),
    (7,  9,  7, '2025-12-04', '2025-12-25', NULL),
    (8, 11,  2, '2025-12-09', '2025-12-30', NULL),
    (9,  3,  5, '2025-12-15', '2026-01-05', '2025-12-28'),
    (10, 10, 3, '2026-01-07', '2026-01-28', NULL),
    (11, 13, 6, '2026-01-12', '2026-02-02', NULL),
    (12, 5,  7, '2026-01-19', '2026-02-09', '2026-02-05');
