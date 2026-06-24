# yêu cầu bổ sung để agent AI thực hiện việc auto-research tốt hơn.
- Luôn luôn lấy các rule tiêu chuẩn về CLEAN ARCHTECTURE làm nền tảng.
- Khi gặp tình huống phức tạp mà không giải quyết được, hoặc có thể có phương án giải quyết nhưng có thể ảnh hưởng đến các modules khác hoặc tạo ra smells thì nên dừng lại.
- Những code với prompt hoặc các câu fix cứng thì nên hãy trích ra và viết với dạng hằng số với hằng kiểu IN_HOA, nếu dùng ở nhiều nơi thì nên tạo ra file constant để dùng chung, hoặc nếu có cấp độ tổ chức thì nên tạo config với dạng json mổ tả rõ.
- 
