-- How many seats each user holds in each show, for the per-user limit.
--
-- A reservation increments this row with a conditional upsert (only if the new total stays within
-- the limit). The row lock that upsert takes makes all of one user's requests for one show take
-- turns, so ten parallel requests cannot all pass a limit of four. Other users never touch this
-- row, so it adds no contention between buyers.

CREATE TABLE user_show_holdings (
    user_id     text    NOT NULL,
    show_id     uuid    NOT NULL REFERENCES shows (id),
    seat_count  integer NOT NULL CHECK (seat_count >= 0),
    PRIMARY KEY (user_id, show_id)
);

-- Seats sold before this table existed.
INSERT INTO user_show_holdings (user_id, show_id, seat_count)
SELECT user_id, show_id, count(*)
  FROM seats
 WHERE status <> 'available'
 GROUP BY user_id, show_id;
