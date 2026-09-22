import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import App from "../App";

function fillForm(sceneVal: string, notesVal: string) {
  fireEvent.change(screen.getByLabelText("场次"), { target: { value: sceneVal } });
  fireEvent.change(screen.getByLabelText("备注"), { target: { value: notesVal } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: /领取镜号|重试领取镜号/ }));
}

beforeEach(() => localStorage.clear());

describe("镜号发放页（真实 API 联调）", () => {
  it("提交后展示镜号并出现在已发放列表", async () => {
    render(<App />);
    const s = `vitest-ui-${crypto.randomUUID()}`;
    fillForm(s, "开场");
    submit();
    expect(await screen.findByTestId("result-card")).toHaveTextContent("镜号 #1");
    await waitFor(() =>
      expect(screen.getByTestId("issued-table")).toHaveTextContent("开场"),
    );
  });

  it("注入故障后保留待重试操作，重试取回同一镜号且不重复占号", async () => {
    render(<App />);
    const s = `vitest-ui-${crypto.randomUUID()}`;
    fillForm(s, "雨夜追车");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/));
    submit();

    // 失败反馈 + 待重试操作保留
    expect(await screen.findByTestId("error-unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("pending-banner")).toBeInTheDocument();

    // 重试（注入标志仍在，但服务端只触发一次）
    submit();
    const card = await screen.findByTestId("result-card");
    expect(card).toHaveTextContent("镜号 #1");
    expect(card).toHaveTextContent("重放结果");
    expect(screen.queryByTestId("pending-banner")).not.toBeInTheDocument();

    // 只发放了一个号码
    await waitFor(() =>
      expect(screen.getAllByTestId("issued-row")).toHaveLength(1),
    );
  });

  it("同一标识换内容给出冲突反馈，可作为新操作重新提交", async () => {
    render(<App />);
    const s = `vitest-ui-${crypto.randomUUID()}`;
    fillForm(s, "版本A");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/));
    submit();
    await screen.findByTestId("error-unavailable");

    // 保留同一操作标识，修改内容后重试 -> 409
    fireEvent.change(screen.getByLabelText("备注"), { target: { value: "版本B" } });
    fireEvent.click(screen.getByLabelText(/注入提交后故障/)); // 关掉注入
    submit();
    expect(await screen.findByTestId("error-conflict")).toBeInTheDocument();

    // 以新操作重新提交 -> 拿到下一个连续号码
    fireEvent.click(screen.getByRole("button", { name: "以新操作重新提交" }));
    expect(await screen.findByTestId("result-card")).toHaveTextContent("镜号 #2");
  });

  it("刷新页面后待重试操作仍保留", async () => {
    const s = `vitest-ui-${crypto.randomUUID()}`;
    const first = render(<App />);
    fillForm(s, "爆破戏");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/));
    submit();
    await screen.findByTestId("error-unavailable");
    first.unmount();

    // 模拟重新打开页面
    render(<App />);
    expect(screen.getByTestId("pending-banner")).toBeInTheDocument();
    expect(screen.getByLabelText("场次")).toHaveValue(s);
    submit();
    expect(await screen.findByTestId("result-card")).toHaveTextContent("镜号 #1");
  });

  it("切换场次后重试展示冲突并保留原待重试操作，以新操作提交才为新场次发号", async () => {
    const sceneA = `vitest-ui-A-${crypto.randomUUID()}`;
    const sceneB = `vitest-ui-B-${crypto.randomUUID()}`;
    render(<App />);
    fillForm(sceneA, "吊臂全景");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/));
    submit();
    expect(await screen.findByTestId("error-unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("pending-banner")).toBeInTheDocument();

    // 误切场次并改备注后重试 -> 冲突反馈（指向首次提交），原待重试操作保留
    fillForm(sceneB, "轨道近景");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/)); // 关掉注入
    submit();
    const conflict = await screen.findByTestId("error-conflict");
    expect(conflict).toHaveTextContent(`首次提交：场次 ${sceneA}`);
    expect(conflict).toHaveTextContent("镜号 #1");
    expect(screen.getByTestId("pending-banner")).toBeInTheDocument();

    // 新场次未被发号
    await waitFor(() =>
      expect(screen.getByText("暂无已发放镜号")).toBeInTheDocument(),
    );

    // 选择“以新操作重新提交”后才为新场次领取号码（该场次首个号）
    fireEvent.click(screen.getByRole("button", { name: "以新操作重新提交" }));
    const card = await screen.findByTestId("result-card");
    expect(card).toHaveTextContent("镜号 #1");
    expect(card).toHaveTextContent(`场次 ${sceneB}`);
    await waitFor(() =>
      expect(screen.getAllByTestId("issued-row")).toHaveLength(1),
    );
  });

  it("冲突后刷新页面仍保留原待重试操作，可找回原场次已生效的镜号", async () => {
    const sceneA = `vitest-ui-A-${crypto.randomUUID()}`;
    const sceneB = `vitest-ui-B-${crypto.randomUUID()}`;
    const first = render(<App />);
    fillForm(sceneA, "吊臂全景");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/));
    submit();
    await screen.findByTestId("error-unavailable");

    // 误切场次重试 -> 冲突
    fillForm(sceneB, "轨道近景");
    fireEvent.click(screen.getByLabelText(/注入提交后故障/)); // 关掉注入
    submit();
    await screen.findByTestId("error-conflict");
    first.unmount();

    // 重新打开页面：恢复的仍是原场次、原备注的待重试操作
    render(<App />);
    expect(screen.getByTestId("pending-banner")).toBeInTheDocument();
    expect(screen.getByLabelText("场次")).toHaveValue(sceneA);
    expect(screen.getByLabelText("备注")).toHaveValue("吊臂全景");

    // 原请求重试 -> 取回原场次已生效的 1 号（重放，不重复占号）
    submit();
    const card = await screen.findByTestId("result-card");
    expect(card).toHaveTextContent("镜号 #1");
    expect(card).toHaveTextContent("重放结果");
    expect(card).toHaveTextContent(`场次 ${sceneA}`);
    await waitFor(() =>
      expect(screen.getAllByTestId("issued-row")).toHaveLength(1),
    );
  });
});
