# Kickoff Lab

Platform analisis Fantasy Premier League — proyeksi poin, fixture, rating tim,
dan susunan skuad — yang datanya **diperbarui otomatis dua kali sehari** lewat
GitHub Actions, lalu di-deploy ke GitHub Pages.

## Cara kerja

```
scripts/fetch_data.py   -> tarik data terbaru (FPL resmi, statistik gaya Opta, fixture)
scripts/build_data.py   -> jalankan model (rating tim, xP, menit bermain, dst.) -> dist/fpl_data.json
scripts/build_page.py   -> suntik data ke site/template.html -> dist/index.html
```

`.github/workflows/deploy.yml` menjalankan ketiga langkah itu setiap hari jam
**08:00 dan 18:00 UTC** (mengikuti jadwal refresh sumber datanya), setiap ada
push ke `main`, dan bisa juga dipicu manual dari tab **Actions**. Hasilnya
otomatis ter-publish ke GitHub Pages — tidak ada langkah manual yang perlu
diulang setiap gameweek.

Skuad yang kamu susun di halaman tersimpan di **browser kamu sendiri**
(localStorage), bukan di repo — jadi aman dan privat, tapi juga berarti hanya
tersimpan di browser/perangkat itu (ada tombol "Salin kode" di halaman untuk
memindahkan skuad ke perangkat lain).

## Setup sekali di awal

1. **Buat repo GitHub** bernama `kickoff-lab` (public), kosong — jangan centang
   "Add a README" saat membuat, supaya tidak bentrok dengan file di paket ini.

2. **Push paket ini** ke repo tersebut. Paket sudah berisi git repo dengan
   satu commit awal, jadi tinggal tambah remote dan push:
   ```bash
   cd kickoff-lab
   git remote add origin https://github.com/<username>/kickoff-lab.git
   git push -u origin main
   ```

3. **Aktifkan GitHub Pages via Actions**: buka repo di GitHub →
   **Settings → Pages** → bagian "Build and deployment" → **Source**, pilih
   **GitHub Actions** (bukan "Deploy from a branch"). Kalau workflow pertama
   sudah pernah berhasil jalan, opsi ini biasanya sudah otomatis terisi.

4. **Jalankan workflow pertama kali**: buka tab **Actions** di repo →
   pilih workflow "Refresh data & deploy" → **Run workflow** (tombol di kanan
   atas). Ini juga otomatis berjalan sendiri setelah push pertama.

5. Setelah workflow selesai (sekitar 1-2 menit), situs bisa diakses di:
   ```
   https://<username>.github.io/kickoff-lab/
   ```
   (URL persisnya juga muncul di Settings → Pages setelah deploy pertama).

Setelah setup ini, tidak ada yang perlu dilakukan lagi — data akan terus
diperbarui otomatis dua kali sehari selamanya, mengikuti musim dan gameweek
yang sedang berjalan tanpa tahun/GW yang di-hardcode di kode.

## Menjalankan secara lokal (opsional, untuk pengujian)

```bash
pip install -r requirements.txt
python scripts/fetch_data.py --out data/raw
python scripts/build_data.py --raw data/raw --out dist/fpl_data.json
python scripts/build_page.py --template site/template.html --data dist/fpl_data.json --out dist/index.html
python -m http.server 8000 --directory dist   # buka http://localhost:8000
```

## Sumber data

- [olbauday/FPL-Elo-Insights](https://github.com/olbauday/FPL-Elo-Insights) —
  data resmi FPL + statistik pertandingan gaya Opta + rating Elo klub
  (diperbarui pemilik repo dua kali sehari, 07:30/17:30 UTC).
- [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League) —
  daftar lengkap 380 fixture musim ini beserta Fixture Difficulty Rating resmi FPL.

## Struktur repo

```
scripts/fetch_data.py    # tarik data mentah -> data/raw/ (di-gitignore, dibuat ulang tiap run)
scripts/build_data.py    # data mentah -> model -> dist/fpl_data.json
scripts/build_page.py    # template + data -> dist/index.html
site/template.html       # halaman (HTML/CSS/JS) — ini yang diedit kalau mau ubah tampilan/fitur
site/favicon.svg         # ikon situs
.github/workflows/deploy.yml
```
