# deploy/diagnosis — 机器级排障工具归档（2026-11-14 重构归位）

一次性排障产物，从 deploy/ 根归档到此处：**不是日常启停脚本**，只在对应症状出现时使用。
逐件交代（背景细节见 docs/部署手册 §6 排障坑表 与 docs/开发日志 对应日条目）：

| 文件 | 症状 | 用法/判读 |
|---|---|---|
| `NioSelectorTest.java` + `nio-test.cmd` | Neo4j 进程在、bolt 不应答 | `nio-test.cmd` 回环自检 WEPoll 事件投递：SELECTOR-OK=正常（瞬时状态），SELECTOR-DEAF=机器级失聪（重启/查驱动/关 FlClash 后再测）。建议未开代理时测，一次分清「瞬时」vs「环境」。 |
| `fix-hosts-localhost.ps1` | JDK13+ WEPoll selector 不唤醒（bolt 广播地址被反解成别的主机名） | 一次性 hosts 修复：确保 `127.0.0.1 localhost` 是 hosts 首行（Typora 条目曾污染反解）。**注意：脚本内日志路径硬编码为旧项目目录，复用时先改路径。** |
| `grant-createglobal.ps1` | 绿色版 PostgreSQL initdb/启动报 1314（Global\ 命名空间共享内存，非提权 UAC 令牌无权创建） | 一次性授权 SeCreateGlobalPrivilege（LsaAddAccountRights），重登生效。授完即不用再看。 |

`deploy/grant-elevate.bat`（原 .cmd 的批处理重制版）留在 deploy/ 根——它属于**安装流程**
（start.sh 的 PG 自降权前置），不属于排障归档。
