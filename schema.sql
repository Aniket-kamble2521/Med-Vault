-- Users: authentication + basic emergency summary fields (MVP).
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  full_name TEXT NOT NULL DEFAULT '',
  blood_group TEXT NOT NULL DEFAULT '',
  allergies TEXT NOT NULL DEFAULT '',
  medications TEXT NOT NULL DEFAULT '',
  conditions TEXT NOT NULL DEFAULT '',
  portal_role TEXT NOT NULL DEFAULT 'patient',
  onboarding_done INTEGER NOT NULL DEFAULT 0,
  theme_accent TEXT NOT NULL DEFAULT '#3b82f6',
  theme_mode TEXT NOT NULL DEFAULT 'light',
  doctor_specialty TEXT NOT NULL DEFAULT '',
  doctor_phone TEXT NOT NULL DEFAULT '',
  doctor_clinic TEXT NOT NULL DEFAULT '',
  doctor_bio TEXT NOT NULL DEFAULT '',
  UNIQUE(username, portal_role)
);

-- Uploaded files (pdf/images).
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'Uncategorized',
  category_confidence REAL NOT NULL DEFAULT 0.0,
  doc_category TEXT NOT NULL DEFAULT '',
  doc_source TEXT NOT NULL DEFAULT '',
  extracted_text TEXT NOT NULL DEFAULT '',
  file_size INTEGER,
  file_type TEXT,
  uploaded_by TEXT DEFAULT 'Patient',
  ai_summary TEXT DEFAULT '',
  uploaded_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- One-time emergency tokens with expiry.
CREATE TABLE IF NOT EXISTS emergency_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  token TEXT UNIQUE NOT NULL,
  expiry_time INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  used_at INTEGER,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_files_user_id ON files(user_id);
CREATE INDEX IF NOT EXISTS idx_emergency_tokens_token ON emergency_tokens(token);
CREATE INDEX IF NOT EXISTS idx_emergency_tokens_expiry ON emergency_tokens(expiry_time);

-- Modular tables for upcoming features (Phase-2 onward).
CREATE TABLE IF NOT EXISTS family_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_user_id INTEGER NOT NULL,
  profile_name TEXT NOT NULL,
  relationship TEXT NOT NULL DEFAULT 'self',
  age INTEGER,
  emergency_contact TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS medical_timeline_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  event_date TEXT NOT NULL,
  event_type TEXT NOT NULL,
  description TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS prescriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  medicine_name TEXT NOT NULL,
  dosage TEXT NOT NULL,
  frequency TEXT NOT NULL,
  doctor_name TEXT DEFAULT '',
  start_date TEXT DEFAULT '',
  end_date TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS doctors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  specialization TEXT DEFAULT '',
  contact TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  doctor_id INTEGER,
  appointment_at TEXT NOT NULL,
  reason TEXT DEFAULT '',
  visit_notes TEXT DEFAULT '',
  referral_note TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'scheduled',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (doctor_id) REFERENCES doctors(id)
);

CREATE TABLE IF NOT EXISTS vaccinations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  vaccine_name TEXT NOT NULL,
  dose_info TEXT DEFAULT '',
  due_date TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS allergies_registry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  allergy_name TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'mild',
  notes TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  remind_at TEXT NOT NULL,
  reminder_type TEXT NOT NULL DEFAULT 'general',
  is_done INTEGER NOT NULL DEFAULT 0,
  dosage TEXT DEFAULT '',
  instructions TEXT DEFAULT '',
  med_image TEXT DEFAULT '',
  snooze_until TEXT DEFAULT NULL,
  follow_up_sent INTEGER DEFAULT 0,
  repeat_enabled INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS vitals_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  logged_at TEXT NOT NULL,
  bp_systolic INTEGER,
  bp_diastolic INTEGER,
  sugar REAL,
  heart_rate INTEGER,
  weight REAL,
  symptoms TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS doctor_profiles (
  user_id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL DEFAULT '',
  profile_photo TEXT NOT NULL DEFAULT '',
  gender TEXT NOT NULL DEFAULT '',
  dob TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  specialty TEXT NOT NULL DEFAULT '',
  sub_specialty TEXT NOT NULL DEFAULT '',
  registration_number TEXT NOT NULL DEFAULT '',
  medical_council TEXT NOT NULL DEFAULT '',
  years_experience INTEGER NOT NULL DEFAULT 0,
  highest_qualification TEXT NOT NULL DEFAULT '',
  college_university TEXT NOT NULL DEFAULT '',
  hospital_clinic TEXT NOT NULL DEFAULT '',
  position TEXT NOT NULL DEFAULT '',
  consultation_fee REAL NOT NULL DEFAULT 0.0,
  languages_spoken TEXT NOT NULL DEFAULT '',
  working_days TEXT NOT NULL DEFAULT '',
  consultation_hours TEXT NOT NULL DEFAULT '',
  timezone TEXT NOT NULL DEFAULT '',
  online_consultation INTEGER NOT NULL DEFAULT 0,
  offline_consultation INTEGER NOT NULL DEFAULT 0,
  bio TEXT NOT NULL DEFAULT '',
  expertise TEXT NOT NULL DEFAULT '',
  certifications TEXT NOT NULL DEFAULT '',
  awards TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS doctor_leaves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_user_id INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (doctor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS prescription_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_user_id INTEGER NOT NULL,
  template_name TEXT NOT NULL,
  medicine_name TEXT NOT NULL,
  dosage TEXT NOT NULL DEFAULT '',
  frequency TEXT NOT NULL DEFAULT '',
  duration_days INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (doctor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS doctor_patient_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_user_id INTEGER NOT NULL,
  patient_name TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  FOREIGN KEY (doctor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS doctor_reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_user_id INTEGER NOT NULL,
  patient_name TEXT NOT NULL,
  title TEXT NOT NULL,
  remind_at TEXT NOT NULL,
  is_done INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (doctor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS doctor_activity_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_user_id INTEGER NOT NULL,
  activity_type TEXT NOT NULL,
  description TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (doctor_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS patient_profiles (
  user_id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL DEFAULT '',
  age INTEGER NOT NULL DEFAULT 0,
  gender TEXT NOT NULL DEFAULT '',
  dob TEXT NOT NULL DEFAULT '',
  blood_group TEXT NOT NULL DEFAULT '',
  height REAL NOT NULL DEFAULT 0.0,
  weight REAL NOT NULL DEFAULT 0.0,
  phone TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  emergency_contact_name TEXT NOT NULL DEFAULT '',
  emergency_contact_number TEXT NOT NULL DEFAULT '',
  existing_diseases TEXT NOT NULL DEFAULT '',
  current_medications TEXT NOT NULL DEFAULT '',
  allergies TEXT NOT NULL DEFAULT '',
  previous_surgeries TEXT NOT NULL DEFAULT '',
  family_medical_history TEXT NOT NULL DEFAULT '',
  smoking_status TEXT NOT NULL DEFAULT '',
  alcohol_consumption TEXT NOT NULL DEFAULT '',
  exercise_frequency TEXT NOT NULL DEFAULT '',
  sleep_duration REAL NOT NULL DEFAULT 0.0,
  diet_preference TEXT NOT NULL DEFAULT '',
  water_intake REAL NOT NULL DEFAULT 0.0,
  occupation TEXT NOT NULL DEFAULT '',
  preferred_language TEXT NOT NULL DEFAULT '',
  preferred_consultation_mode TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS patient_notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  category TEXT NOT NULL, -- 'Appointments', 'Medicines', 'Reports', 'Prescriptions', 'Payments', 'Doctor Messages', 'General'
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS medicine_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  reminder_id INTEGER,
  medicine_name TEXT NOT NULL,
  dosage TEXT NOT NULL,
  taken_at INTEGER NOT NULL,
  instructions TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'taken', -- 'taken', 'skipped'
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS emergency_access_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_user_id INTEGER NOT NULL,
  doctor_name TEXT NOT NULL DEFAULT '',
  patient_user_id INTEGER NOT NULL,
  accessed_at INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (doctor_user_id) REFERENCES users(id),
  FOREIGN KEY (patient_user_id) REFERENCES users(id)
);



