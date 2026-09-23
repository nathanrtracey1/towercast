#!/usr/bin/env python3
"""
Option A Helper: Cloudflare Quick Tunnel for Overcast.
Generates an instant, free public HTTPS URL and updates config.json automatically.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def check_cloudflared():
    path = shutil.which("cloudflared")
    if not path:
        local_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "cloudflared")
        for candidate in [local_bin, "/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"]:
            if os.path.exists(candidate):
                return candidate
    return path

def main():
    print("=" * 65)
    print("🌐 TOWERCAST - OPTION A TUNNEL (FOR OVERCAST)")
    print("=" * 65)
    print("Overcast's servers require a publicly accessible HTTPS podcast feed.")
    print("Cloudflare Quick Tunnels provide a free, secure, instant HTTPS URL")
    print("pointing directly to your local TowerCast server on your Mac.\n")

    cloudflared_bin = check_cloudflared()
    if not cloudflared_bin:
        print("⚠️  'cloudflared' is not currently installed.")
        print("You can install it easily with Homebrew by running:")
        print("    brew install cloudflared\n")
        print("Once installed, run this script again or run:")
        print("    python3 tunnel.py\n")
        print("Alternatively, if you already have Tailscale installed:")
        print("    tailscale funnel 8080")
        print("and set 'public_base_url' in config.json to your Tailscale Funnel URL.")
        return

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    port = config.get("server_port", 8080)
    print(f"Starting free Cloudflare Quick Tunnel to http://localhost:{port}...")
    print("Press Ctrl+C to stop.\n")

    cmd = [cloudflared_bin, "tunnel", "--url", f"http://localhost:{port}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    tunnel_url = None
    try:
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            # Look for *.trycloudflare.com
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match and not tunnel_url:
                tunnel_url = match.group(0)
                print("\n" + "✨" * 30)
                print(f"🎉 TUNNEL ACTIVE!")
                print(f"Public URL:        {tunnel_url}")
                print(f"Podcast Feed URL:  {tunnel_url}/feed.xml")
                print("✨" * 30 + "\n")
                print("👉 Paste this into Overcast (Add by URL):")
                print(f"   {tunnel_url}/feed.xml\n")

                # Update config.json with the tunnel URL
                config["public_base_url"] = tunnel_url
                with open(CONFIG_PATH, "w") as f:
                    json.dump(config, f, indent=2)
                print("Updated config.json public_base_url automatically.\n")
            elif not tunnel_url:
                if "error" in line.lower() or "retrying" in line.lower():
                    sys.stdout.write(line)
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping tunnel...")
        proc.terminate()

if __name__ == "__main__":
    main()
