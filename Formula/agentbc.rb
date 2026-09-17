class Agentbc < Formula
  include Language::Python::Virtualenv

  desc "Local-first task control plane for Codex, Claude Code, and Hermes"
  homepage "https://github.com/roway49/agent-bridge-connect"
  url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.4A/agentbc-1.0.4a1.tar.gz"
  version "1.0.4a1"
  sha256 "6a295a6c9d31a028bba84a5db209d92224b5cf1a4f4d38906f3a1e1442b23e19"
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
    assert_match "agentbc 1.0.4a1", shell_output("#{bin}/agentbc --version")
    system bin/"agentbc", "--help"
  end
end
