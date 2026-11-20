# berth-bench

隔离运行环境的对比实验脚本：同一台机器上比较 Firecracker、Cloud Hypervisor、gVisor、Kata 和 runc 的起机与恢复时间、每台内存开销、编译速度、单机密度、内存回收、用量记账、换盘、同机来回延迟等。

这个库只放实验脚本和干跑用的 workflow，用 GitHub 托管跑机（自带 KVM，嵌套虚拟化）先把每条路径走通；跑机上量出来的数不作数，真数在裸金属上量。

- 脚本：`bench/`（`run-all.sh` 一口气跑；`bench.py <实验>` 单跑一组；`report.py <结果目录>` 出表）
- 干跑只跑 `bench/dryrun.txt` 里点到的实验和段落
