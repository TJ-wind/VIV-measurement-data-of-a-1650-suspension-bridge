"""绘制一个涡振事件的风速与加速度传感器时程。

推荐十分钟涡振时段：python plot_viv_event.py
指定事件：python plot_viv_event.py path/to/event_folder
局部时段：python plot_viv_event.py --start "2013-02-06 00:52:40" --end "2013-02-06 00:53:10"

输入为本数据集的 MATLAB v7.3 文件，包含表格 data 和绝对时间列 time。
风速各通道的零值仅在绘图时设为 NaN，保留原时间轴。
不滤波、不插值、不降采样；空缺数据保留为 NaN，分段边界处断开曲线。
"""

from pathlib import Path
import argparse

import h5py
import numpy as np
import pandas as pd
import matplotlib

# 只导出图片，不弹出图窗，适合在服务器或 GitHub 下载后的本地环境中运行。
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# 常用设置：路径相对于脚本，发布到 GitHub 后无需修改本机盘符。
script_dir = Path(__file__).resolve().parent
example_event = "2013_02_05_21_40_to_2013_02_06_04_00"
wind_columns = [f"UA{i}{axis}" for i in range(1, 7) for axis in "xyz"]
wind_sensors = [1, 2, 5, 6]  # 图中显示的风速测点；读取 MAT 时仍保留完整列名校验
acc_columns = [f"AC{i}" for i in range(10, 19)]
line_colors = ["#3C5488", "#00A087", "#E64B35"]  # 蓝、绿、朱红
pdf_point_limit = 200_000  # 整张图超过此数量的有效点时自动使用 PNG
image_dpi = 300


def read_mat(path, columns):
    """读取本数据集保存的 table + datetime，返回按时间索引的数据表。"""
    with h5py.File(path, "r") as handle:
        if handle["data"].attrs.get("MATLAB_class") != b"table":
            raise ValueError(f"{path.name} 中的 data 必须是 MATLAB 表格。")

        # MATLAB 将 table 保存为 MCOS 对象，而非普通二维数组。
        # 这些引用位置对应本数据集的保存格式（MATLAB R2023b）。
        # 保留这一小段专用读取逻辑，其余绘图部分只使用普通 DataFrame。
        refs = handle["#subsystem#/MCOS"][()].ravel()
        if len(refs) != 14:
            raise ValueError(f"{path.name} 的 table 存储结构与示例数据不一致。")
        name_refs = handle[refs[9]][()].ravel()
        names = ["".join(chr(int(c)) for c in handle[r][()].ravel()) for r in name_refs]
        if names != ["time"] + columns:
            raise ValueError(f"{path.name} 的列名或列顺序不匹配：{names}")

        data_refs = handle[refs[4]][()].ravel()
        values = np.column_stack([handle[r][()].ravel() for r in data_refs[1:]])
        stored_time = handle[refs[2]][()].ravel()
        # datetime 内部保存 Unix 毫秒，部分文件采用 real/imag 双分量表示。
        if stored_time.dtype.names == ("real", "imag"):
            milliseconds = stored_time["real"]
            residual = stored_time["imag"]
        elif stored_time.dtype.kind == "f":
            milliseconds = stored_time
            residual = np.zeros_like(milliseconds)
        else:
            raise ValueError(f"{path.name} 的时间编码不受支持。")
        if len(milliseconds) == 0 or not np.isfinite(milliseconds).all():
            raise ValueError(f"{path.name} 的时间列为空或包含无效值。")
        # 先减去首值，避免大时间戳直接相加引入采样间隔的精度损失。
        origin = pd.to_datetime(milliseconds[0], unit="ms")
        elapsed = (milliseconds - milliseconds[0] + residual - residual[0]) / 1000
        time = pd.DatetimeIndex(origin + pd.to_timedelta(elapsed, unit="s"), name="time")

    if not time.is_monotonic_increasing or time.has_duplicates:
        raise ValueError(f"{path.name} 的时间存在倒序或重复。")
    return pd.DataFrame(values, index=time, columns=columns)


def read_event(folder, suffix, columns, start=None, end=None):
    """按文件名顺序读取十分钟片段，缺失值及文件间的缺口不进行补齐。"""
    files = sorted(folder.glob(f"*_{suffix}.mat"))
    if not files:
        raise FileNotFoundError(f"{folder} 中没有 *_{suffix}.mat 文件。")
    parts = []
    previous_end = None
    for path in files:
        segment_start = pd.to_datetime(path.name[:16], format="%Y_%m_%d_%H_%M")
        # 指定局部时段时跳过无关文件，避免读取整场事件。
        if start is not None and segment_start + pd.Timedelta(minutes=10) <= start:
            continue
        if end is not None and segment_start >= end:
            continue
        frame = read_mat(path, columns)
        if start is not None:
            frame = frame.loc[frame.index >= start]
        if end is not None:
            frame = frame.loc[frame.index < end]
        if frame.empty:
            continue
        if previous_end is not None:
            if frame.index[0] <= previous_end:
                raise ValueError(f"{path.name} 与前一个片段的时间重叠。")
            # 插入一行 NaN 仅用于断开画线，避免跨越缺失片段连接两端。
            gap_time = frame.index[0] - pd.Timedelta(1, unit="ns")
            parts.append(pd.DataFrame(np.nan, index=[gap_time], columns=columns))
        parts.append(frame)
        previous_end = frame.index[-1]
    if not parts:
        raise ValueError(f"指定时段内没有 {suffix} 数据。")
    return pd.concat(parts)


def plot_channels(frame, groups, titles, ylabel, heading, time_limits, output_path, image_format, share_y=False):
    """每个测点或跨中截面占一行，各通道使用相同配色和绝对时间坐标。"""
    # 每个通道单独占一格，避免密集时程相互遮挡。
    fig, axes = plt.subplots(len(groups), 3, sharex=True, squeeze=False,
                             figsize=(12, 1.65 * len(groups) + 1), layout="constrained")
    if share_y == "all":
        # 九个加速度通道共同确定纵轴范围和刻度，便于直接比较振幅。
        for ax in axes.flat[1:]:
            ax.sharey(axes[0, 0])
    elif share_y:
        # x、y 两列的水平分量共用纵轴，第三列的竖向分量单独共用纵轴。
        for ax in axes[:, :2].flat[1:]:
            ax.sharey(axes[0, 0])
        for ax in axes[1:, 2]:
            ax.sharey(axes[0, 2])
    x = mdates.date2num(frame.index.to_numpy())
    for row, channels, title in zip(axes, groups, titles):
        for column, (ax, channel, color) in enumerate(zip(row, channels, line_colors)):
            ax.plot(x, frame[channel].to_numpy(), color=color, linewidth=.55)
            ax.set_title(channel, loc="left", fontsize=10, fontweight="bold", color=color)
            if column == 0:
                ax.set_ylabel(title + "\n" + ylabel)
            ax.grid(axis="y", color="#DDE2E8", linewidth=.5)
            ax.set_axisbelow(True)
            ax.spines[["top", "right"]].set_visible(False)
            if not np.isfinite(frame[channel].to_numpy()).any():
                ax.text(.5, .5, "No displayed samples", transform=ax.transAxes,
                        ha="center", color="#777777", fontsize=8)
    for ax in axes[-1]:
        ax.set_xlim(mdates.date2num(time_limits))
        locator = mdates.AutoDateLocator(minticks=3, maxticks=5)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.set_xlabel("Time")
    fig.suptitle(heading, fontsize=12)

    # 稀疏图使用矢量 PDF；密集原始时程改用 PNG，减少文件体积和打开时间。
    plotted_columns = [channel for group in groups for channel in group]  # 仅统计实际绘制的通道
    point_count = int(np.isfinite(frame[plotted_columns].to_numpy()).sum())
    chosen_format = image_format
    if chosen_format == "auto":
        chosen_format = "pdf" if point_count <= pdf_point_limit else "png"
    destination = output_path.with_suffix("." + chosen_format)
    fig.savefig(destination, dpi=image_dpi, facecolor="white")
    plt.close(fig)
    print(f"Saved: {destination.name} ({point_count:,} points, {destination.stat().st_size / 1024**2:.2f} MB)")


def main():
    parser = argparse.ArgumentParser(description="Plot wind and acceleration time histories from MAT files.")
    parser.add_argument("event_dir", nargs="?", type=Path, default=None,
                        help="Directory containing the MAT files for one event.")
    parser.add_argument("--output", type=Path, default=script_dir / "figures", help="Output directory.")
    parser.add_argument("--start", type=pd.Timestamp, help='Start time, e.g. "2013-02-06 00:52:40".')
    parser.add_argument("--end", type=pd.Timestamp, help="End time (exclusive).")
    parser.add_argument("--format", choices=["auto", "pdf", "png"], default="auto")
    args = parser.parse_args()
    if args.event_dir is None:
        args.event_dir = script_dir / example_event
        # 直接运行时展示已核对的十分钟涡振时段，显式传入目录或时段时按指定参数绘图。
        if args.start is None and args.end is None:
            args.start = pd.Timestamp("2013-02-06 00:50:00")
            args.end = args.start + pd.Timedelta(minutes=10)
    if not args.event_dir.is_dir():
        parser.error(f"Event directory does not exist: {args.event_dir}")
    if args.start is not None and args.end is not None and args.start >= args.end:
        parser.error("--end must be later than --start.")

    wind = read_event(args.event_dir, "Windtimehistory", wind_columns, args.start, args.end)
    # 各风速通道严格等于 0 的点不画出；用 NaN 断线，保留时间及其他通道的非零值。
    wind = wind.mask(wind.eq(0))
    acceleration = read_event(args.event_dir, "VIVtimehistory", acc_columns, args.start, args.end)
    # 两张图共用绝对时间范围；两类采样率可以不同，不进行重采样或逐行配对。
    time_limits = [min(wind.index[0], acceleration.index[0]),
                   max(wind.index[-1], acceleration.index[-1])]
    if time_limits[0] == time_limits[1]:
        time_limits[1] += pd.Timedelta(seconds=1)
    output_dir = args.output  # 图片直接保存到输出目录，不再创建事件子文件夹
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"],
                         "font.size": 9, "axes.linewidth": .6, "pdf.fonttype": 42,
                         "xtick.direction": "out", "ytick.direction": "out",
                         "agg.path.chunksize": 10000})

    wind_groups = [[f"UA{i}{axis}" for axis in "xyz"] for i in wind_sensors]
    wind_titles = [f"UA{i}" for i in wind_sensors]
    acc_groups = [["AC10", "AC11", "AC12"], ["AC13", "AC14", "AC15"], ["AC16", "AC17", "AC18"]]
    acc_titles = ["Quarter span", "Midspan", "Three-quarter span"]
    plot_channels(wind, wind_groups, wind_titles, "Wind speed (m/s)",
                  "Wind sensor time histories | x: west, y: north, z: vertical | zero values omitted", time_limits, output_dir / "wind_time_history", args.format, share_y=True)
    plot_channels(acceleration, acc_groups, acc_titles, r"Acceleration (cm/s$^2$)",
                  "Acceleration time histories | left/right: vertical, centre: lateral", time_limits, output_dir / "acceleration_time_history", args.format, share_y="all")
    print(f"Output: {output_dir}")


if __name__ == "__main__":
    main()

