import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Form, Input, Button, Row, Col, Skeleton, message } from 'antd';
import { LoadingOutlined, ExclamationCircleOutlined } from '@ant-design/icons';
import { PaymentMethod } from './PaymentMethod';

const CheckoutPage: React.FC = () => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const fetchCart = async () => {
      try {
        const response = await axios.get('https://api.mangodelivery.com/cart');
        if (response.data.length === 0) {
          setError('No items in cart');
          message.error('No items in cart');
        }
      } catch (err) {
        setError('Failed to fetch cart');
        message.error('Failed to fetch cart');
      } finally {
        setLoading(false);
      }
    };

    fetchCart();
  }, []);

  if (loading) return <Skeleton active />;
  if (error) return <ExclamationCircleOutlined style={{ fontSize: 24 }} />;

  return (
    <div style={{ padding: '24px' }}>
      <h1>Checkout</h1>
      <Form layout="vertical">
        <Form.Item name="name" label="Name" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="address" label="Address" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <PaymentMethod />
        <Form.Item>
          <Button type="primary" htmlType="submit" href="/confirmation">
            Place Order
          </Button>
        </Form.Item>
      </Form>
    </div>
  );
};

export default CheckoutPage;
