-- Idempotency: a retried request with the same key returns the original reservation and never
-- creates a second one.
--
-- The unique constraint is what makes "exactly once" hold under concurrency: when two requests
-- with the same key race, the second INSERT waits on this index until the first commits, then
-- sees the conflict and replays the original.
--
-- The keys are scoped per user, so two users can never collide or read each other's bookings.
--
-- The defaults keep this migration safe while the previous version is still serving (Railway
-- overlaps deploys): its INSERTs carry no key, so each row gets a unique key nobody will send.
-- Existing rows get one too, because a volatile default is evaluated per row.

ALTER TABLE reservations
    ADD COLUMN idempotency_key text  NOT NULL DEFAULT ('auto:' || gen_random_uuid()::text),
    ADD COLUMN request_hash    bytea NOT NULL DEFAULT '\x'::bytea,
    ADD CONSTRAINT reservations_user_key_unique UNIQUE (user_id, idempotency_key);
