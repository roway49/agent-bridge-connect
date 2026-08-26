class Agentbc < Formula
  include Language::Python::Virtualenv

  desc "Local-first task control plane for Codex, Claude Code, and Hermes"
  homepage "https://github.com/roway49/agent-bridge-connect"
  url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.3A/agentbc-1.0.3a1.tar.gz"
  version "1.0.3a1"
  sha256 "5342affc02902429e0eda1d7bcc5aa284c2f957662309c8a469ee294a56cf8d7"
  license "MIT"
  revision 1

  bottle do
    root_url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.3A"
    sha256 cellar: :any_skip_relocation, all: "9c1475516fc98e7d0a539178572c7692a25e5c4e29289b666db14d17e7b52e36"
  end

  depends_on "python"

  patch do
    url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.3A/agentbc-1.0.3a1-homebrew-update-zero-write.patch"
    sha256 "30edbb613bfad2a4cb09ccdba122f92be390e52da71c524dee4d267c1b2220a9"
  end

  def install
    virtualenv_install_with_resources
  end

  service do
    run [opt_bin/"agentbc", "runner", "serve"]
    keep_alive true
    log_path var/"log/agentbc-runner.log"
    error_log_path var/"log/agentbc-runner.log"
  end

  def caveats
    <<~EOS
      Run `agentbc setup` after install or upgrade to discover executors and
      install the version-matched Codex, Claude, and Hermes skills.
      Start the managed Runner with `brew services start agentbc`.
    EOS
  end

  test do
    assert_match "agentbc 1.0.3a1", shell_output("#{bin}/agentbc --version")
    system bin/"agentbc", "--help"
    blocked_home = testpath/"blocked-home"
    blocked_home.write "not a directory"
    assert_match "homebrew_update_required", shell_output("HOME=#{blocked_home} #{bin}/agentbc update")
  end
end
