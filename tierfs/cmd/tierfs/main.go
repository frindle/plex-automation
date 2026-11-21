// Command tierfs is a read-only FUSE overlay for offloaded media files.
package main

import (
	"log"
	"os"
	"os/signal"
	"syscall"

	"tierfs"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("tierfs: ")
	cfg, err := tierfs.ConfigFromEnv(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	srv, err := tierfs.New(cfg)
	if err != nil {
		log.Fatal(err)
	}
	if err := srv.Start(); err != nil {
		log.Fatal(err)
	}
	log.Printf("mounted at %s, manifests in %s", cfg.Mount, cfg.Manifests)

	sig := make(chan os.Signal, 4)
	signal.Notify(sig, syscall.SIGHUP, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		for s := range sig {
			if s == syscall.SIGHUP {
				log.Printf("SIGHUP: reloading manifests")
				srv.Reload()
				continue
			}
			log.Printf("%v: unmounting", s)
			if err := srv.Close(); err != nil {
				log.Printf("unmount: %v", err)
			}
			return
		}
	}()
	srv.Wait()
	log.Printf("stopped")
}
