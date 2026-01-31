package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
)

func checksum(url, app string) (string, string, string) {
	// local_sha
	f, _ := os.Open(app)
	h := sha256.New()
	io.Copy(h, f)
	f.Close()
	local_sha := hex.EncodeToString(h.Sum(nil))

	// remote_sha
	resp, err := http.Get(url)
	if err != nil {
		os.Exit(1)
	}
	defer resp.Body.Close()
	var m map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		os.Exit(1)
	}
	remote_sha := m["sha256"]
	return local_sha, remote_sha, m["version"]
}

func update(ver, app string) { // do later
	var loc, rem, rver string
	if ver == "main" {
		loc, rem, rver = checksum("https://raw.githubusercontent.com/gateway19/pgral/refs/heads/main/info.json", app)
	} else {
		loc, rem, rver = checksum("https://raw.githubusercontent.com/gateway19/pgral/refs/heads/dev/info.json", app)
	}
	if loc != rem {
		// update
		fmt.Println("Обновляемся до ", ver, rver)
		update_url := fmt.Sprintf("https://github.com/gateway19/pgral/releases/download/%s/pgral.exe", rver)
		resp, err := http.Get(update_url)
		if err != nil {
			return
		}
		defer resp.Body.Close()

		out, err := os.Create(app)
		if err != nil {
			return
		}
		defer out.Close()

		io.Copy(out, resp.Body)
	}
}

func main() {
	u := flag.String("u", "", "URL")
	v := flag.String("v", "", "beta/main")
	flag.Parse()
	pgralPath := filepath.Join(filepath.Dir(os.Args[0]), "pgral.exe")
	u_argument := fmt.Sprintf("\"%s\"", *u)

	if *v == "main" || *v == "beta" {
		update(*v, pgralPath)
	}
	if u_argument != "\"\"" {
		exec.Command("cmd", "/c", "start", "", pgralPath, "-u", u_argument).Start()
	} else {
		exec.Command("cmd", "/c", "start", "", pgralPath).Start()
	}
}
