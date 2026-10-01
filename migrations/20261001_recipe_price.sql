-- Periksa schema terlebih dahulu; jalankan sekali setelah backup.
ALTER TABLE resep ADD COLUMN harga_jual_per_potong DECIMAL(15,2) NOT NULL DEFAULT 0;
-- Harga setiap resep diisi oleh pengguna melalui Kelola Resep.
