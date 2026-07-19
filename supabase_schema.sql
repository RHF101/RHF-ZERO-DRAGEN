-- ============================================================
-- RHF Assistant — Supabase schema
-- Jalankan ini di Supabase Dashboard > SQL Editor
-- Aman dijalankan ulang (pakai IF NOT EXISTS / DROP...CREATE)
-- ============================================================

-- ============================================================
-- CATATAN: Chat history ("ingatan" AI) SEKARANG pakai Firebase
-- Realtime Database (path: sessions/{uid}/{mode}), BUKAN Supabase.
-- Tabel `messages` versi lama tidak dipakai lagi. Kalau kamu pernah
-- menjalankan schema versi sebelumnya dan mau bersih-bersih, jalankan
-- manual: DROP TABLE IF EXISTS messages;
-- (sengaja tidak di-drop otomatis di sini biar tidak menghapus data
-- tanpa sepengetahuanmu)
-- ============================================================

-- Metadata gambar yang diupload user (file asli ada di Storage bucket 'user-images')
create table if not exists images (
  id bigint generated always as identity primary key,
  user_id text not null,               -- Firebase UID, kunci akses per akun
  storage_path text not null,          -- path di bucket, mis: {user_id}/{uuid}.jpg
  mime_type text not null default 'image/jpeg',
  created_at timestamptz not null default now()
);

create index if not exists idx_images_user
  on images (user_id, created_at desc);

-- File non-gambar (PDF, docx, xlsx, txt, dll — semua tipe file lain)
create table if not exists files (
  id bigint generated always as identity primary key,
  user_id text not null,               -- Firebase UID, kunci akses per akun
  storage_path text not null,          -- path di bucket 'user-files'
  file_name text not null,
  mime_type text not null default 'application/octet-stream',
  size_bytes bigint not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists idx_files_user
  on files (user_id, created_at asc);  -- asc: file terlama duluan, buat auto-prune kuota

alter table files enable row level security;

-- Row Level Security: aktifkan tapi akses tetap lewat service_role key dari
-- backend (bypass RLS otomatis), jadi ini cuma pengaman tambahan kalau
-- publishable/anon key kepakai langsung dari client di masa depan.
alter table images enable row level security;

-- ============================================================
-- Storage bucket untuk gambar — PRIVATE (bukan public)
-- Jalankan lewat Dashboard > Storage > New bucket:
--   name: user-images
--   public: false
-- Backend pakai service_role key + signed URL buat generate link sementara,
-- jadi nggak perlu policy storage khusus (service_role bypass RLS storage juga).
-- ============================================================

-- ============================================================
-- Storage bucket untuk file non-gambar — PRIVATE juga
-- Jalankan lewat Dashboard > Storage > New bucket:
--   name: user-files
--   public: false
-- Kuota per akun ditegakkan di level aplikasi (api/index.py), bukan di
-- Supabase — 500MB akun biasa, 2GB khusus email admin.
-- ============================================================
