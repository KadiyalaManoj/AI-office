import React from 'react';
import { Radio, Form } from 'antd';

const PaymentMethod: React.FC = () => {
  const { Option } = Radio;

  return (
    <Form.Item name="paymentMethod" label="Payment Method" rules={[{ required: true }]}>
      <Radio.Group>
        <Option value="creditCard">Credit Card</Option>
        <Option value="paypal">PayPal</Option>
        <Option value="applePay">Apple Pay</Option>
      </Radio.Group>
    </Form.Item>
  );
};

export default PaymentMethod;
