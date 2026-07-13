CREATE TYPE photo_type AS ENUM (
  'film_color_negative',
  'film_color_positive',
  'film_bw',
  'digital'
);

CREATE TABLE photo_group (
  id                   BIGSERIAL PRIMARY KEY,
  name                 TEXT        NOT NULL,
  type                 photo_type  NOT NULL,
  film_stock           TEXT,
  iso                  INTEGER,
  camera               TEXT,
  lens                 TEXT,
  developer            TEXT,
  fixer                TEXT,
  dev_number           INTEGER,
  roll_number          INTEGER,
  size                 INTEGER,        -- 135, 120, 4x5, etc.
  number_of_exposures  INTEGER,
  expiration           TEXT,
  dates_shot           TEXT,
  date_developed       DATE,
  dev_time_mins        NUMERIC(5, 2),
  dev_temp_f           NUMERIC(5, 2),
  fixer_time_mins      NUMERIC(5, 2),
  dev_adjustments      TEXT    NOT NULL DEFAULT '',
  scanner              TEXT,
  notes                TEXT    NOT NULL DEFAULT '',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE photo (
  id               BIGSERIAL PRIMARY KEY,
  group_id         BIGINT  NOT NULL REFERENCES photo_group(id) ON DELETE CASCADE,
  s3_key           TEXT    NOT NULL UNIQUE,  -- e.g. groups/42/photos/001.jpg
  sequence_in_roll INTEGER NOT NULL,
  width            INTEGER NOT NULL,
  height           INTEGER NOT NULL,
  title            TEXT    NOT NULL DEFAULT '',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_photo_group_id        ON photo(group_id);
CREATE INDEX idx_photo_group_sequence  ON photo(group_id, sequence_in_roll);
CREATE INDEX idx_group_date_developed  ON photo_group(date_developed DESC NULLS LAST);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

CREATE TRIGGER photo_group_updated_at
  BEFORE UPDATE ON photo_group
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER photo_updated_at
  BEFORE UPDATE ON photo
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
