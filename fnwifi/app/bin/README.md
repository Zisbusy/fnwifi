# fnwifi 自带二进制目录

本应用为"自包含"架构:hostapd / dnsmasq / iw 等运行依赖
全部使用静态编译的二进制,放在本目录下,按架构分子目录:

```
bin/
├── aarch64/     # ARM64(斐讯N1、RK3588 等)
│   ├── hostapd
│   ├── dnsmasq
│   └── iw
├── x86_64/      # 通用 x86 平台
│   └── ...
└── README.md
```

运行时优先使用本目录下的二进制;不存在时自动回退到系统 PATH。
因此即使某个平台暂时没放静态二进制,应用也能用系统自带的命令运行。

静态二进制建议使用 musl 交叉编译(hostapd/dnsmasq/iw 均为标准 autotools 项目):
- hostapd:   https://w1.fi/hostapd/
- dnsmasq:   https://thekelleys.org.uk/dnsmasq/
- iw:        https://git.kernel.org/pub/scm/linux/kernel/git/jberg/iw

或用 CI(GitHub Actions)为 aarch64 / x86_64 各编一套,发布时打进 fpk。
