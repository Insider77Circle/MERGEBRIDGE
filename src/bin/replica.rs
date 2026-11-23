//! MERGEBRIDGE prototype replica process.
//!
//! One process = one replica of the shared workspace document (Automerge).
//! The "host" and "guest" are the same binary run with different roles/dirs.
//! They sync with the Automerge sync protocol over a Unix domain socket
//! (standing in for coop's vsock relay / SSH boundary — no new listener on
//! any network interface; the socket exists only during a sync session).
//!
//! Document model (per the MERGEBRIDGE synthesis):
//!   ROOT
//!     "files": Map
//!        "<name>": Text object                     (text files)
//!        "<name>": Map { sha256: Str, size: Int }  (binary files —
//!                    hash reference ONLY; content lives in a per-replica
//!                    blob store and crosses the channel out-of-band,
//!                    verified by hash on receipt)
//!   No secret-bearing path exists in this schema, by construction.
//!
//! Commands are read as lines on stdin; each command's output ends with a
//! line "DONE". State persists to <dir>/doc.automerge after every mutation;
//! the workspace is projected to <dir>/ws/ after every mutation/sync.

use automerge::sync::{Message, State as SyncState, SyncDoc};
use automerge::transaction::Transactable;
use automerge::{AutoCommit, ObjId, ObjType, ReadDoc, ScalarValue, Value, ROOT};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};

const FRAME_SYNC: u8 = 0x01;
const FRAME_BLOB_LIST: u8 = 0x02;
const FRAME_BLOB_DATA: u8 = 0x03;
const FRAME_BLOB_DONE: u8 = 0x04;

struct Replica {
    role: String,
    dir: PathBuf,
    doc: AutoCommit,
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex::encode(h.finalize())
}

/// Deterministic fixture/created binary content: an ASCII marker repeated
/// through a sha256 chain, so tests can scan the CRDT bytes for the marker.
fn gen_binary(seed: &str, size: usize) -> Vec<u8> {
    let marker = b"MERGEBRIDGE-BINARY-MARKER";
    let mut out = Vec::with_capacity(size + 64);
    let mut block = Sha256::digest(seed.as_bytes()).to_vec();
    while out.len() < size {
        out.extend_from_slice(marker);
        out.extend_from_slice(&block);
        block = Sha256::digest(&block).to_vec();
    }
    out.truncate(size);
    out
}

impl Replica {
    fn doc_path(&self) -> PathBuf {
        self.dir.join("doc.automerge")
    }
    fn blob_dir(&self) -> PathBuf {
        self.dir.join("blobs")
    }
    fn ws_dir(&self) -> PathBuf {
        self.dir.join("ws")
    }

    fn load_or_new(dir: &Path) -> AutoCommit {
        let p = dir.join("doc.automerge");
        if p.exists() {
            let bytes = std::fs::read(&p).expect("read doc");
            AutoCommit::load(&bytes).expect("load doc")
        } else {
            AutoCommit::new()
        }
    }

    fn files_map(&mut self) -> ObjId {
        if let Some((Value::Object(_), id)) = self.doc.get(&ROOT, "files").ok().flatten() {
            return id;
        }
        self.doc
            .put_object(&ROOT, "files", ObjType::Map)
            .expect("create files map")
    }

    fn persist(&mut self) {
        std::fs::create_dir_all(&self.dir).unwrap();
        std::fs::write(self.doc_path(), self.doc.save()).unwrap();
    }

    fn store_blob(&self, bytes: &[u8]) -> String {
        let hash = sha256_hex(bytes);
        std::fs::create_dir_all(self.blob_dir()).unwrap();
        let p = self.blob_dir().join(&hash);
        if !p.exists() {
            std::fs::write(&p, bytes).unwrap();
        }
        hash
    }

    fn blob_hashes(&self) -> Vec<String> {
        let mut v = Vec::new();
        if let Ok(rd) = std::fs::read_dir(self.blob_dir()) {
            for e in rd.flatten() {
                v.push(e.file_name().to_string_lossy().to_string());
            }
        }
        v.sort();
        v
    }

    /// Binary references currently in the document: name -> (sha256, size).
    fn binary_refs(&mut self) -> BTreeMap<String, (String, i64)> {
        let mut out = BTreeMap::new();
        let files = match self.doc.get(&ROOT, "files") {
            Ok(Some((Value::Object(_), id))) => id,
            _ => return out,
        };
        for name in self.doc.keys(&files) {
            if let Ok(Some((_, oid))) = self.doc.get(&files, &name) {
                if let Ok(ObjType::Map) = self.doc.object_type(&oid) {
                    let sha = match self.doc.get(&oid, "sha256") {
                        Ok(Some((Value::Scalar(s), _))) => match s.as_ref() {
                            ScalarValue::Str(t) => t.to_string(),
                            _ => continue,
                        },
                        _ => continue,
                    };
                    let size = match self.doc.get(&oid, "size") {
                        Ok(Some((Value::Scalar(s), _))) => match s.as_ref() {
                            ScalarValue::Int(i) => *i,
                            _ => 0,
                        },
                        _ => 0,
                    };
                    out.insert(name, (sha, size));
                }
            }
        }
        out
    }

    /// Project the document to <dir>/ws/ (full projection; fixture scale).
    fn project(&mut self) {
        let ws = self.ws_dir();
        std::fs::create_dir_all(&ws).unwrap();
        // Remove files no longer in the document.
        let mut wanted = Vec::new();
        if let Ok(Some((Value::Object(_), files))) = self.doc.get(&ROOT, "files") {
            let names: Vec<String> = self.doc.keys(&files).collect();
            for name in names {
                wanted.push(name.clone());
                if let Ok(Some((_, oid))) = self.doc.get(&files, &name) {
                    match self.doc.object_type(&oid) {
                        Ok(ObjType::Text) => {
                            let content = self.doc.text(&oid).unwrap_or_default();
                            std::fs::write(ws.join(&name), content).unwrap();
                        }
                        Ok(ObjType::Map) => {
                            if let Some((sha, _)) = self.binary_refs().get(&name) {
                                let blob = self.blob_dir().join(sha);
                                if blob.exists() {
                                    std::fs::copy(&blob, ws.join(&name)).unwrap();
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        if let Ok(rd) = std::fs::read_dir(&ws) {
            for e in rd.flatten() {
                let n = e.file_name().to_string_lossy().to_string();
                if !wanted.contains(&n) {
                    let _ = std::fs::remove_file(e.path());
                }
            }
        }
    }

    fn after_mutation(&mut self) {
        self.persist();
        self.project();
    }

    fn dump(&mut self) -> String {
        let mut lines = Vec::new();
        if let Ok(Some((Value::Object(_), files))) = self.doc.get(&ROOT, "files") {
            let mut names: Vec<String> = self.doc.keys(&files).collect();
            names.sort();
            for name in names {
                if let Ok(Some((_, oid))) = self.doc.get(&files, &name) {
                    match self.doc.object_type(&oid) {
                        Ok(ObjType::Text) => {
                            let content = self.doc.text(&oid).unwrap_or_default();
                            lines.push(format!(
                                "FILE {} TEXT {}",
                                name,
                                content.replace('\\', "\\\\").replace('\n', "\\n")
                            ));
                        }
                        Ok(ObjType::Map) => {
                            if let Some((sha, size)) = self.binary_refs().get(&name) {
                                lines.push(format!(
                                    "FILE {} BINARY sha256={} size={}",
                                    name, sha, size
                                ));
                            }
                        }
                        _ => lines.push(format!("FILE {} OTHER", name)),
                    }
                }
            }
        }
        lines.join("\n")
    }

    fn heads_hex(&mut self) -> String {
        let mut hs: Vec<String> = self
            .doc
            .get_heads()
            .iter()
            .map(|h| h.to_string())
            .collect();
        hs.sort();
        hs.join(",")
    }

    // ---- fixture / edit operations ----

    fn cmd_init_fixture(&mut self) -> String {
        let files = self.files_map();
        let t = self
            .doc
            .put_object(&files, "notes.txt", ObjType::Text)
            .unwrap();
        self.doc
            .splice_text(&t, 0, 0, "The quick brown fox jumps over the lazy dog.\n")
            .unwrap();
        let blob = gen_binary("mergebridge-logo-v1", 4096);
        let hash = self.store_blob(&blob);
        let m = self
            .doc
            .put_object(&files, "logo.bin", ObjType::Map)
            .unwrap();
        self.doc.put(&m, "sha256", hash.as_str()).unwrap();
        self.doc.put(&m, "size", blob.len() as i64).unwrap();
        self.after_mutation();
        format!("fixture created; logo.bin sha256={}", hash)
    }

    fn cmd_insert(&mut self, file: &str, pos: usize, text: &str) -> String {
        let files = self.files_map();
        match self.doc.get(&files, file) {
            Ok(Some((_, oid))) => {
                self.doc.splice_text(&oid, pos, 0, text).unwrap();
                self.after_mutation();
                format!("inserted {} chars into {} at {}", text.chars().count(), file, pos)
            }
            _ => format!("ERROR no such text file: {}", file),
        }
    }

    fn cmd_create_text(&mut self, file: &str, content: &str) -> String {
        let files = self.files_map();
        let t = self.doc.put_object(&files, file, ObjType::Text).unwrap();
        self.doc.splice_text(&t, 0, 0, content).unwrap();
        self.after_mutation();
        format!("created text file {}", file)
    }

    fn cmd_create_binary(&mut self, file: &str, seed: &str) -> String {
        let blob = gen_binary(seed, 2048);
        let hash = self.store_blob(&blob);
        let files = self.files_map();
        let m = self.doc.put_object(&files, file, ObjType::Map).unwrap();
        self.doc.put(&m, "sha256", hash.as_str()).unwrap();
        self.doc.put(&m, "size", blob.len() as i64).unwrap();
        self.after_mutation();
        format!("created binary {} sha256={}", file, hash)
    }

    // ---- sync session ----

    fn run_sync_session(&mut self, stream: &mut UnixStream) -> std::io::Result<String> {
        stream.set_read_timeout(None)?;
        let mut reader = BufReader::new(stream.try_clone()?);
        let mut state = SyncState::new();
        // Phase 1: Automerge sync protocol until both sides have nothing.
        loop {
            let my_msg = self.doc.sync().generate_sync_message(&mut state);
            let had_msg = my_msg.is_some();
            match my_msg {
                Some(m) => write_frame(stream, FRAME_SYNC, &m.encode())?,
                None => write_frame(stream, FRAME_SYNC, &[])?,
            }
            let (tag, payload) = read_frame(&mut reader)?;
            if tag != FRAME_SYNC {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "expected sync frame",
                ));
            }
            if !payload.is_empty() {
                let msg = Message::decode(&payload)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string()))?;
                self.doc
                    .sync()
                    .receive_sync_message(&mut state, msg)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e.to_string()))?;
            }
            if !had_msg && payload.is_empty() {
                break;
            }
        }
        // Phase 2: blob exchange (out-of-band content transfer, hash-verified).
        // Announce blobs I hold; send blobs the peer's document references but
        // the peer does not hold; then read until the peer is done.
        let my_blobs = self.blob_hashes();
        write_frame(stream, FRAME_BLOB_LIST, my_blobs.join("\n").as_bytes())?;
        let peer_refs: Vec<String> = self
            .binary_refs()
            .values()
            .map(|(s, _)| s.clone())
            .collect();
        // We do not yet know the peer's blob list when sending; send every
        // referenced blob we hold that we cannot prove the peer has is wrong
        // in general, so: read the peer's list first is impossible in this
        // symmetric write-then-read shape. Instead: send blobs for refs in
        // OUR doc; the peer keeps the ones it needs and ignores duplicates
        // (content-addressed store makes re-receipt harmless).
        let _ = peer_refs;
        for hash in self.binary_refs().values().map(|(s, _)| s.clone()) {
            let p = self.blob_dir().join(&hash);
            if p.exists() {
                let bytes = std::fs::read(&p)?;
                let mut payload = hash.into_bytes();
                payload.push(b'\n');
                payload.extend_from_slice(&bytes);
                write_frame(stream, FRAME_BLOB_DATA, &payload)?;
            }
        }
        write_frame(stream, FRAME_BLOB_DONE, &[])?;
        // Read phase: peer's list, blobs, done.
        loop {
            let (tag, payload) = read_frame(&mut reader)?;
            match tag {
                FRAME_BLOB_LIST => {
                    // Informational in this prototype: receipt is idempotent.
                }
                FRAME_BLOB_DATA => {
                    let nl = payload.iter().position(|&b| b == b'\n').unwrap_or(0);
                    let claimed = String::from_utf8_lossy(&payload[..nl]).to_string();
                    let bytes = &payload[nl + 1..];
                    let actual = sha256_hex(bytes);
                    if actual != claimed {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::InvalidData,
                            format!("blob hash mismatch: claimed {} got {}", claimed, actual),
                        ));
                    }
                    self.store_blob(bytes);
                }
                FRAME_BLOB_DONE => break,
                _ => {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "unexpected frame in blob phase",
                    ))
                }
            }
        }
        self.after_mutation();
        Ok(format!("SYNCED heads={}", self.heads_hex()))
    }

    fn cmd_sync(&mut self, socket: &Path) -> String {
        if self.role == "host" {
            let _ = std::fs::remove_file(socket);
            let listener = UnixListener::bind(socket).expect("bind socket");
            match listener.accept() {
                Ok((mut stream, _)) => match self.run_sync_session(&mut stream) {
                    Ok(s) => s,
                    Err(e) => format!("ERROR sync: {}", e),
                },
                Err(e) => format!("ERROR accept: {}", e),
            }
        } else {
            // The host binds asynchronously from the guest's point of view;
            // retry the connect briefly instead of racing the bind (a stale
            // socket file from a previous session accepts no connections).
            let mut last_err = None;
            let mut stream = None;
            for _ in 0..200 {
                match UnixStream::connect(socket) {
                    Ok(s) => {
                        stream = Some(s);
                        break;
                    }
                    Err(e) => {
                        last_err = Some(e);
                        std::thread::sleep(std::time::Duration::from_millis(50));
                    }
                }
            }
            match stream {
                Some(mut s) => match self.run_sync_session(&mut s) {
                    Ok(s) => s,
                    Err(e) => format!("ERROR sync: {}", e),
                },
                None => format!("ERROR connect: {}", last_err.unwrap()),
            }
        }
    }
}

fn write_frame(w: &mut impl Write, tag: u8, payload: &[u8]) -> std::io::Result<()> {
    w.write_all(&[tag])?;
    w.write_all(&(payload.len() as u32).to_be_bytes())?;
    w.write_all(payload)?;
    w.flush()
}

fn read_frame(r: &mut impl Read) -> std::io::Result<(u8, Vec<u8>)> {
    let mut tag = [0u8; 1];
    r.read_exact(&mut tag)?;
    let mut len = [0u8; 4];
    r.read_exact(&mut len)?;
    let n = u32::from_be_bytes(len) as usize;
    let mut payload = vec![0u8; n];
    r.read_exact(&mut payload)?;
    Ok((tag[0], payload))
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut role = String::new();
    let mut dir = PathBuf::new();
    let mut socket = PathBuf::new();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--role" => {
                role = args[i + 1].clone();
                i += 2;
            }
            "--dir" => {
                dir = PathBuf::from(&args[i + 1]);
                i += 2;
            }
            "--socket" => {
                socket = PathBuf::from(&args[i + 1]);
                i += 2;
            }
            _ => {
                i += 1;
            }
        }
    }
    assert!(role == "host" || role == "guest", "--role host|guest");
    std::fs::create_dir_all(&dir).unwrap();
    let doc = Replica::load_or_new(&dir);
    let mut rep = Replica { role, dir, doc };
    rep.project();

    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let line = line.unwrap();
        // Strip only the line terminator — trailing spaces in an insert
        // payload are data, not framing.
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            continue;
        }
        let mut parts = line.splitn(2, ' ');
        let cmd = parts.next().unwrap_or("");
        let rest = parts.next().unwrap_or("");
        let out = match cmd {
            "init-fixture" => rep.cmd_init_fixture(),
            "insert" => {
                // insert <file> <pos> <text...>
                let mut p = rest.splitn(3, ' ');
                let f = p.next().unwrap_or("");
                let pos: usize = p.next().unwrap_or("0").parse().unwrap_or(0);
                let text = p.next().unwrap_or("");
                rep.cmd_insert(f, pos, text)
            }
            "create-text" => {
                let mut p = rest.splitn(2, ' ');
                let f = p.next().unwrap_or("");
                let c = p.next().unwrap_or("");
                rep.cmd_create_text(f, c)
            }
            "create-binary" => {
                let mut p = rest.splitn(2, ' ');
                let f = p.next().unwrap_or("");
                let s = p.next().unwrap_or("seed");
                rep.cmd_create_binary(f, s)
            }
            "sync" => rep.cmd_sync(&socket),
            "dump" => rep.dump(),
            "heads" => format!("heads={}", rep.heads_hex()),
            "quit" => {
                writeln!(stdout, "BYE").unwrap();
                writeln!(stdout, "DONE").unwrap();
                stdout.flush().unwrap();
                return;
            }
            other => format!("ERROR unknown command: {}", other),
        };
        writeln!(stdout, "{}", out).unwrap();
        writeln!(stdout, "DONE").unwrap();
        stdout.flush().unwrap();
    }
}
