import { expect, test } from "@playwright/test";

test("提交后故障又切换场次：重试得 409 且保留待重试，恢复首次内容取回 1 号，新操作在新场次从 1 开始", async ({
  page,
}) => {
  await page.goto("/");

  await page.getByLabel("场次").fill("STAGE-A");
  await page.getByLabel("备注").fill("吊臂全景");
  await page.getByLabel(/注入提交后故障/).check();
  await page.getByRole("button", { name: "领取镜号" }).click();
  await expect(page.getByTestId("error-unavailable")).toBeVisible();

  // 误切换到 STAGE-B，改备注、关闭故障注入后重试
  await page.getByLabel("场次").fill("STAGE-B");
  await page.getByLabel("备注").fill("轨道近景");
  await page.getByLabel(/注入提交后故障/).uncheck();
  await page.getByRole("button", { name: /重试领取镜号/ }).click();

  // 冲突反馈明确指向首次提交的 STAGE-A，原待重试操作仍保留
  await expect(page.getByTestId("error-conflict")).toBeVisible();
  await expect(page.getByTestId("conflict-existing")).toContainText("STAGE-A");
  await expect(page.getByTestId("conflict-existing")).toContainText("吊臂全景");
  await expect(page.getByTestId("pending-banner")).toBeVisible();

  // 恢复首次提交内容并重试 -> 取回 STAGE-A 的 1 号（重放）
  await page.getByRole("button", { name: "恢复首次提交内容并重试" }).click();
  await expect(page.getByTestId("result-card")).toContainText("镜号 #1");
  await expect(page.getByTestId("result-card")).toContainText("重放结果");
  await expect(page.getByTestId("pending-banner")).toHaveCount(0);

  // STAGE-B 从未占号：在该场次以新操作提交仍从 1 开始
  await page.getByLabel("场次").fill("STAGE-B");
  await page.getByRole("button", { name: "领取镜号" }).click();
  await expect(page.getByTestId("result-card")).toContainText("镜号 #1");
});
