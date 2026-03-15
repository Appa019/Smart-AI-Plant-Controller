-- Hoya Pet — Supabase PostgreSQL Schema
-- Run this in the Supabase SQL Editor to create all tables

-- Sensor readings from ESP32
CREATE TABLE sensor_readings (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    temperature REAL,
    humidity REAL,
    soil_moisture INTEGER,
    soil_raw INTEGER,
    soil_status TEXT
);
CREATE INDEX idx_sensor_ts ON sensor_readings(timestamp DESC);

-- Users (authentication)
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Email verification codes
CREATE TABLE email_codes (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    code TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_email_codes_email ON email_codes(email);

-- Pump commands (replaces pending_pump.json + last_pump.json)
CREATE TABLE pump_commands (
    id BIGSERIAL PRIMARY KEY,
    seconds INTEGER NOT NULL,
    requested_by TEXT,
    requested_at TIMESTAMPTZ DEFAULT NOW(),
    executed_at TIMESTAMPTZ,
    status TEXT DEFAULT 'pending'
);
CREATE INDEX idx_pump_status ON pump_commands(status);

-- User preferences (replaces user_prefs.json + soil_cal.json)
CREATE TABLE user_prefs (
    user_email TEXT PRIMARY KEY,
    active_slot INTEGER DEFAULT 1,
    soil_cal JSONB DEFAULT '{}'::jsonb,
    last_reminder_time TIMESTAMPTZ
);

-- Plant slots (replaces plant_profile.json + pet_config.json + pet_state.json)
CREATE TABLE plant_slots (
    id BIGSERIAL PRIMARY KEY,
    user_email TEXT NOT NULL,
    slot_id INTEGER NOT NULL,
    plant_profile JSONB DEFAULT '{}'::jsonb,
    pet_config JSONB DEFAULT '{}'::jsonb,
    pet_state JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_email, slot_id)
);
CREATE INDEX idx_plant_slots_user ON plant_slots(user_email);

-- Pet generation jobs (async processing via Edge Functions)
CREATE TABLE pet_jobs (
    id BIGSERIAL PRIMARY KEY,
    user_email TEXT NOT NULL,
    slot_id INTEGER NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_pet_jobs_status ON pet_jobs(status);
