-- Provider reservations have an owner so a late request cannot clear another worker's cooldown.
ALTER TABLE provider_state ADD COLUMN lease_token TEXT;
