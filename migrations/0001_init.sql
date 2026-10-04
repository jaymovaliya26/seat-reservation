-- Initial schema.
--
-- The rule that makes double-selling impossible: every seat is exactly one row, and that row
-- has one status and at most one owner. Counts are derived from these rows, so
-- available + held + confirmed = total_seats holds by construction.

CREATE TABLE shows (
    id              uuid        PRIMARY KEY,
    name            text        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    price_paise     bigint      NOT NULL CHECK (price_paise > 0),
    per_user_limit  integer     NOT NULL CHECK (per_user_limit >= 1),
    total_seats     integer     NOT NULL CHECK (total_seats >= 1),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reservations (
    id            uuid        PRIMARY KEY,
    show_id       uuid        NOT NULL REFERENCES shows (id),
    user_id       text        NOT NULL,
    seats         text[]      NOT NULL CHECK (cardinality(seats) >= 1),
    amount_paise  bigint      NOT NULL CHECK (amount_paise > 0),
    status        text        NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    cancelled_at  timestamptz,
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);

CREATE INDEX reservations_show_user_idx ON reservations (show_id, user_id);

CREATE TABLE seats (
    show_id         uuid        NOT NULL REFERENCES shows (id),
    label           text        NOT NULL,
    -- Order the seats were given in, so responses list A2 before A10.
    position        integer     NOT NULL,
    status          text        NOT NULL DEFAULT 'available'
                                CHECK (status IN ('available', 'held', 'confirmed')),
    reservation_id  uuid        REFERENCES reservations (id),
    user_id         text,
    -- Unused until timed holds exist; here so adding them needs no table rewrite.
    held_until      timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (show_id, label),
    -- An available seat has no owner; a held or confirmed seat always has exactly one.
    CONSTRAINT seats_owner_matches_status CHECK (
        (status = 'available' AND reservation_id IS NULL AND user_id IS NULL)
        OR (status <> 'available' AND reservation_id IS NOT NULL AND user_id IS NOT NULL)
    )
);

CREATE INDEX seats_show_status_idx ON seats (show_id, status);
CREATE INDEX seats_reservation_idx ON seats (reservation_id) WHERE reservation_id IS NOT NULL;
