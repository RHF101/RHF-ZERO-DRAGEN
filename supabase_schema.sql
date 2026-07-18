-- ============================================================
-- RHF Assistant — Supabase schema
-- Jalankan ini di Supabase Dashboard > SQL Editor
-- Aman dijalankan ulang (pakai IF NOT EXISTS / DROP...CREATE)
-- ============================================================

-- Riwayat chat, gantiin Firebase RTDB "sessions/{uid}/{mode}"
create table if not exists messages (
  id bigint generated always as identity primary key,
  user_id text not null,              -- Firebase UID
  mode text not null default 'normal', -- 'normal' atau 'rhf'
  role text not null,                  -- 'user' atau 'assistant'
  content text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_messages_user_mode
  on messages (user_id, mode, created_at desc);

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

-- Row Level Security: aktifkan tapi akses tetap lewat service_role key dari
-- backend (bypass RLS otomatis), jadi ini cuma pengaman tambahan kalau
-- publishable/anon key kepakai langsung dari client di masa depan.
alter table messages enable row level security;
alter table images enable row level security;

-- ============================================================
-- Storage bucket untuk gambar — PRIVATE (bukan public)
-- Jalankan lewat Dashboard > Storage > New bucket:
--   name: user-images
--   public: false
-- Backend pakai service_role key + signed URL buat generate link sementara,
-- jadi nggak perlu policy storage khusus (service_role bypass RLS storage juga).
-- ============================================================
