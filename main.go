package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

func update(ver string) { // do later
	if ver == "main" {
		//main
	} else {
		// dev
	}
}

func main() {
	u := flag.String("u", "", "URL")
	v := flag.String("v", "", "beta/main")
	flag.Parse()
	if *v == "main" || *v == "beta" {
		update(*v)
	}
	pgralPath := filepath.Join(filepath.Dir(os.Args[0]), "pgral.exe")
	u_argument := fmt.Sprintf("\"%s\"", *u)
	exec.Command("cmd", "/c", "start", "", pgralPath, "-u", u_argument).Start()
}
