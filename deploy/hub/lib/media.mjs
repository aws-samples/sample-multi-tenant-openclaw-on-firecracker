// lib/media.mjs — S3 presign(懒加载 client)(#136 拆分)。
// 单例状态唯一定义点(js-split 门3):_s3 只在本文件。函数体逐字搬自 server.mjs。

import { AWS_REGION } from "./config.mjs";

let _s3 = null; // lazy S3 client + presigner
export async function getS3() {
  if (_s3) return _s3;
  const { S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand } = await import("@aws-sdk/client-s3");
  const { getSignedUrl } = await import("@aws-sdk/s3-request-presigner");
  _s3 = {
    client: new S3Client({ region: AWS_REGION }),
    PutObjectCommand, GetObjectCommand, HeadObjectCommand, getSignedUrl,
  };
  return _s3;
}
