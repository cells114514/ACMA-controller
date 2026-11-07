# External components / 外部组件

ACMA-controller contains ACMA execution-layer code extracted and generalized from its original agent application. It does not bundle the agent runtime, model SDKs, model weights, Windows, OpenSSH or Cua Driver binaries/source.

ACMA-controller 包含从原 ACMA 应用提取并通用化的执行层代码，不分发 Agent 运行框架、模型 SDK/权重、Windows、OpenSSH 或 Cua Driver 二进制/源码。

## Cua Driver

- Upstream: https://github.com/trycua/cua/tree/main/libs/cua-driver
- API compatibility baseline: `cua-driver 0.38.0` on Windows. This is a tested protocol baseline, not a claim that every later version is compatible.
- Source reviewed for license boundary: commit `a58b3edd3f38f46134db19e9838b151e1df225fd`.
- License at that reviewed revision: [MIT](https://github.com/trycua/cua/blob/a58b3edd3f38f46134db19e9838b151e1df225fd/LICENSE.md), copyright Cua AI, Inc. The Driver README distinguishes its MIT core from optional differently licensed perception extensions.
- Driver is installed separately. No upstream installer is executed by this project. No perception extension is installed or bundled. Keep upstream license notices when obtaining/distributing upstream components; this project's MIT license does not relicense those components.

Driver 单独安装，本项目不执行上游在线安装脚本，也不安装/分发感知扩展。请遵守所安装具体版本的上游声明；本仓库的 MIT 许可不替代其许可。

## Windows / Hyper-V / OpenSSH

These are separately installed platform/runtime prerequisites under their own terms. The local Python package has no third-party runtime Python dependencies. CI packaging uses setuptools; CI uses GitHub-maintained checkout/setup-python actions.

它们是单独安装的平台前置条件，适用各自条款。Python 运行层仅使用标准库。

## Optional screenshot transforms / 可选截图变换

The `imaging` extra installs Pillow (separately distributed under its upstream license). Base control, queues and VM tools remain standard-library-only. Pillow is not vendored. Batch ordering and screenshot mapping are ACMA implementations; no Anthropic SDK, Computer Use model, API adapter or reference source was incorporated in this revision.

`imaging` 选装项单独安装 Pillow，适用其上游许可；基础控制接口仍仅使用标准库。本次未复制 Anthropic 示例源码，也未添加其SDK、模型或协议适配。
