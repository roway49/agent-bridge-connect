class Agentbc < Formula
  include Language::Python::Virtualenv

  desc "Local-first task control plane for Codex, Claude Code, and Hermes"
  homepage "https://github.com/roway49/agent-bridge-connect"
  url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.3A2/agentbc-1.0.3a2.tar.gz"
  version "1.0.3a2"
  sha256 "4644734013081ef22f5f6c9941f0b75fb3ca60816cd1ddf4d16cde382d18be89"
  license "MIT"

  bottle do
    root_url "https://github.com/roway49/agent-bridge-connect/releases/download/v1.0.3A2"
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "e42f10d5643aa78a406984fa19720fcc4de3f671f50afb9d5b15a0a1a68498ea"
    sha256 cellar: :any_skip_relocation, tahoe: "4ff5fac11796e4790e74e7d85b915325dbc3161e7bb4dedea9909e46def4ca80"
  end

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
    assert_match "agentbc 1.0.3a2", shell_output("#{bin}/agentbc --version")
    system bin/"agentbc", "--help"
  end
end
