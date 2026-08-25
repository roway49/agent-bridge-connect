class Agentbc < Formula
  include Language::Python::Virtualenv

  desc "Local-first task control plane for Codex, Claude Code, and Hermes"
  homepage "https://github.com/roway49/agent-bridge-connect"
  url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.3A/agentbc-1.0.3a1.tar.gz"
  version "1.0.3a1"
  sha256 "5342affc02902429e0eda1d7bcc5aa284c2f957662309c8a469ee294a56cf8d7"
  license "MIT"

  depends_on "python"

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
  end
end
