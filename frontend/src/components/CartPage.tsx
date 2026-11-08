import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Card, Button, List, Row, Col, Skeleton, message } from 'antd';
import { LoadingOutlined, ExclamationCircleOutlined } from '@ant-design/icons';

const CartPage: React.FC = () => {
  const [cart, setCart] = useState<{ [key: string]: any }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [total, setTotal] = useState(0);

  useEffect(() => {
    const fetchCart = async () => {
      try {
        const response = await axios.get('https://api.mangodelivery.com/cart');
        setCart(response.data);
        setTotal(response.data.reduce((acc, item) => acc + item.price * item.quantity, 0));
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
      <h1>Cart</h1>
      <List
        grid={{ gutter: 16, column: 2 }}
        dataSource={cart}
        renderItem={(item) => (
          <List.Item>
            <Card
              title={item.name}
              cover={<img alt={item.name} src={item.image} />}
              actions={[
                <Button key="remove" type="link" onClick={() => console.log('Remove')}>
                  Remove
                </Button>,
              ]}
            >
              <p>Quantity: {item.quantity}</p>
              <p>Price: ${item.price.toFixed(2)}</p>
              <p>Total: ${(item.price * item.quantity).toFixed(2)}</p>
            </Card>
          </List.Item>
        )}
      />
      <Row gutter={16} style={{ marginTop: '24px' }}>
        <Col xs={24} sm={12} md={8} lg={6}>
          <h2>Total: ${total.toFixed(2)}</h2>
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <Button type="primary" block href="/checkout">
            Proceed to Checkout
          </Button>
        </Col>
      </Row>
    </div>
  );
};

export default CartPage;
