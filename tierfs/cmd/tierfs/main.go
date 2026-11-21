// Command tierfs is a read-only FUSE overlay for offloaded media files.
package main

import (
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

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

	done := make(chan struct{})
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
			// Open handles can keep the FUSE loop alive after a lazy
			// unmount, so give it a moment and then exit regardless.
			select {
			case <-done:
			case <-time.After(3 * time.Second):
			}
			log.Printf("stopped")
			os.Exit(0)
		}
	}()
	srv.Wait()
	close(done)
	time.Sleep(time.Second) // let the signal goroutine finish its own exit
	log.Printf("stopped")
}
